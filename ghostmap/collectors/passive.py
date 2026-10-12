"""Passive listeners: what this PC hears on the machine network without sending anything.

- **ARP table**: this PC's own ARP cache, read every 10 s (a local table read; no packet is sent). New
  addresses and an IP that changes MAC (a replaced device or an IP conflict) become events.
- **BOOTP/DHCP** (UDP 67): new or reset modules broadcast a request for an IP. Ghost Map logs it and
  never answers: it is not a BOOTP or DHCP server.
- **Syslog** (UDP 514) and **SNMP traps** (UDP 162): what switches send when they are set to log or trap to
  this PC (ports going down, err-disabled ports, loops, restarts). Ghost Map does not configure the switch.

Each listener binds its port without SO_REUSEADDR, so it never takes packets meant for another program (Rockwell
BOOTP/DHCP Server, a DHCP server, another syslog or trap receiver): if the port is taken, the listener says so
and stays off. LLDP/CDP and the rest of the broadcast traffic need packet capture (planned: pktmon).
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from ghostmap.analysis import passive as rules
from ghostmap.collectors.arp import read_arp_cache
from ghostmap.protocols.oui import mac_vendor
from ghostmap.protocols.passive import PassiveError, parse_bootp, parse_snmp_trap, parse_syslog

log = logging.getLogger(__name__)

LISTENERS = {
    "arp": {"label": "ARP table", "port": None, "what": "Devices this PC talks to or hears, from its own ARP table"},
    "bootp": {"label": "BOOTP / DHCP requests", "port": 67, "what": "Modules with no IP asking for one (never answered)",
              "busy": "another BOOTP or DHCP server (for example Rockwell's BOOTP/DHCP Server tool)"},
    "syslog": {"label": "Switch syslog", "port": 514, "what": "Port down/up, err-disabled ports, loops, restarts",
               "busy": "another syslog server"},
    "traps": {"label": "SNMP traps", "port": 162, "what": "Link down/up, restarts, wrong SNMP community",
              "busy": "another SNMP trap receiver"},
}
ARP_EVERY_S = 10
EVENTS_KEPT = 500
FLOOD_PER_MIN = 60  # events per kind and sender per minute; more are counted, not listed


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, mon: "PassiveMonitor", name: str):
        self.mon, self.name = mon, name

    def datagram_received(self, data, addr):
        self.mon.feed(self.name, data, addr[0])

    def error_received(self, exc):
        log.debug("passive %s: %s", self.name, exc)


class PassiveMonitor:
    """Runs the listeners, keeps the devices heard and the last ``EVENTS_KEPT`` events in memory.

    ``known`` returns ({mac: label}, {mac: ip}, {ip: name}) from the newest scan, so events can name devices.
    With ``simulate`` (the demo), no socket is opened: ``ghostmap.sim.passive`` feeds made-up packets instead."""

    def __init__(self, root: Path, known: Callable[[], tuple[dict, dict, dict]] = lambda: ({}, {}, {}),
                 arp_reader: Callable[[], dict] = read_arp_cache, ports: Optional[dict] = None, simulate: bool = False):
        self.path = Path(root) / "live.json"
        self.known, self.arp_reader, self.simulate = known, arp_reader, simulate
        self.ports = {k: v["port"] for k, v in LISTENERS.items()} | (ports or {})
        self.settings = {k: True for k in LISTENERS}
        if self.path.exists():
            try:
                self.settings |= {k: bool(v) for k, v in json.loads(self.path.read_text(encoding="utf-8")).items() if k in LISTENERS}
            except (OSError, ValueError) as exc:
                log.warning("can't read %s: %s", self.path, exc)
        self.status = {k: {"state": "off", "error": None, "packets": 0, "last": None} for k in LISTENERS}
        self.devices: dict[str, dict] = {}
        self.events: deque = deque(maxlen=EVENTS_KEPT)
        self.seq = 0
        self.started: Optional[float] = None
        self._transports: dict = {}
        self._tasks: set = set()
        self._arp_prev: dict = {}
        self._rate: dict = {}
        self.running = False

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.running, self.started = True, time.time()
        for name in LISTENERS:
            if self.settings[name]:
                await self._open(name)
        if self.simulate:
            from ghostmap.sim.passive import run as sim_run

            self._spawn(sim_run(self))

    async def stop(self) -> None:
        self.running = False
        for name in list(self._transports):
            self._close(name)
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for name in LISTENERS:
            self.status[name]["state"] = "off"

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def set_enabled(self, name: str, on: bool) -> None:
        if name not in LISTENERS:
            raise KeyError(name)
        self.settings[name] = bool(on)
        tmp = self.path.with_name(f"live.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        if not self.running:
            return
        if on and name not in self._transports:
            await self._open(name)
        elif not on:
            self._close(name)

    async def _open(self, name: str) -> None:
        st = self.status[name]
        st.update(state="listening", error=None)
        if name == "arp":
            self._transports[name] = self._spawn(self._arp_loop()) if not self.simulate else "sim"
            return
        if self.simulate:
            self._transports[name] = "sim"
            return
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Proto(self, name), local_addr=("0.0.0.0", self.ports[name]))
        except OSError as exc:
            st.update(state="error", error=self._why(name, exc))
            log.warning("passive %s on UDP %s: %s", name, self.ports[name], st["error"])
            return
        self._transports[name] = transport

    def _why(self, name: str, exc: OSError) -> str:
        port = self.ports[name]
        if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, 10013):
            return f"not allowed to listen on UDP {port}: run Ghost Map as the Windows service (or as administrator / root)"
        if exc.errno in (errno.EADDRINUSE, 10048):
            return f"UDP {port} is in use by {LISTENERS[name].get('busy', 'another program')}; Ghost Map leaves it alone"
        return f"can't listen on UDP {port}: {exc}"

    def _close(self, name: str) -> None:
        t = self._transports.pop(name, None)
        if isinstance(t, asyncio.Task):
            t.cancel()
        elif t is not None and t != "sim":
            t.close()
        self.status[name]["state"] = "off"

    # ------------------------------------------------------------- ARP
    async def _arp_loop(self) -> None:
        while True:
            try:
                now = await asyncio.to_thread(self.arp_reader)
                self.arp_update(now)
            except Exception as exc:  # keep the loop alive; show why
                self.status["arp"].update(state="error", error=f"can't read the ARP table: {exc}")
            await asyncio.sleep(ARP_EVERY_S)

    def arp_update(self, now: dict, at: Optional[float] = None) -> None:
        at = at or time.time()
        known = self.known()[0]
        st = self.status["arp"]
        st.update(packets=st["packets"] + 1, last=at, state="listening", error=None)
        for ev in rules.arp_changes(self._arp_prev, now, known):
            self._add(ev, at)
        for ip, mac in now.items():
            self._seen(ip, mac, "arp", at, known)
        self._arp_prev = dict(now)

    # ------------------------------------------------------------- packets
    def feed(self, name: str, data: bytes, source: str, at: Optional[float] = None) -> Optional[dict]:
        """One datagram heard by listener ``name`` from ``source``: decode, classify, remember."""
        if not self.settings.get(name, False):
            return None
        at = at or time.time()
        st = self.status[name]
        st.update(packets=st["packets"] + 1, last=at)
        known, known_ip, names = self.known()
        who = f"{names[source]} ({source})" if source in names and names[source] != source else source
        try:
            if name == "bootp":
                msg = parse_bootp(data)
                ev = rules.bootp_event(msg, known, known_ip)
                if ev:
                    self._seen(ev["ip"] or "", msg["mac"], "bootp", at, known)
            elif name == "syslog":
                ev = {**rules.syslog_event(parse_syslog(data), who), "source": source}
                self._seen(source, "", "syslog", at, known)
            elif name == "traps":
                ev = {**rules.trap_event(parse_snmp_trap(data), who), "source": source}
                self._seen(source, "", "traps", at, known)
            else:
                return None
        except PassiveError as exc:
            log.debug("passive %s from %s: %s", name, source, exc)
            return None
        return self._add(ev, at) if ev else None

    def _seen(self, ip: str, mac: str, how: str, at: float, known: dict) -> None:
        key = mac or ip
        if not key:
            return
        if not mac:  # a syslog or trap sender: attach to the device that has that IP, if there is one
            key = next((k for k, d in self.devices.items() if d["ip"] == ip), ip)
        elif ip and ip in self.devices and not self.devices[ip]["mac"]:  # heard by IP first, now its MAC is known
            self.devices[mac] = self.devices.pop(ip)
        d = self.devices.setdefault(key, {"ip": ip, "mac": mac, "vendor": mac_vendor(mac) if mac else "", "label": "",
                                          "first": at, "last": at, "sources": []})
        d["last"] = max(d["last"], at)
        if ip:
            d["ip"] = ip
        if mac and not d["mac"]:
            d["mac"], d["vendor"] = mac, mac_vendor(mac)
        d["label"] = known.get(d["mac"], "") if d["mac"] else d["label"]
        if how not in d["sources"]:
            d["sources"].append(how)

    def _add(self, ev: dict, at: float) -> Optional[dict]:
        key = (ev["kind"], ev["source"] or ev["mac"] or ev["ip"], int(at // 60))
        n = self._rate.get(key, 0) + 1
        self._rate = {k: v for k, v in self._rate.items() if k[2] >= int(at // 60) - 1}
        self._rate[key] = n
        if n > FLOOD_PER_MIN:
            if n == FLOOD_PER_MIN + 1:
                ev = rules._ev(ev["kind"], "warn", "passive.flood", f"{key[1]} is sending more than {FLOOD_PER_MIN} {ev['kind']} messages a minute; "
                               "the rest of this minute's are counted, not listed.", "A port flapping, a loop, or a misconfigured device.",
                               source=ev["source"], ip=ev["ip"], mac=ev["mac"])
            else:
                return None
        self.seq += 1
        ev = {**ev, "seq": self.seq, "at": at}
        self.events.append(ev)
        if ev["level"] in ("err", "warn"):
            log.info("live: %s", ev["text"])
        return ev

    # ------------------------------------------------------------- API view
    def snapshot(self, since: int = 0) -> dict:
        return {
            "running": self.running, "simulated": self.simulate, "started": self.started, "seq": self.seq,
            "listeners": [{"name": k, **{x: v[x] for x in ("label", "port", "what")}, "enabled": self.settings[k], **self.status[k]}
                          for k, v in LISTENERS.items()],
            "devices": sorted(self.devices.values(), key=lambda d: -d["last"]),
            "events": [e for e in self.events if e["seq"] > since][-200:],
            "counts": {lvl: sum(1 for e in self.events if e["level"] == lvl) for lvl in ("err", "warn", "info", "ok")},
        }

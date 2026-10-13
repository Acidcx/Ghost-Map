"""Scan orchestration: discovery -> switches -> correlation -> diagnostics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ghostmap.analysis.diagnostics import Thresholds, load_baseline, run_diagnostics
from ghostmap.analysis.topology import build_inventory
from ghostmap.collectors import arp, discovery, portstats, stratix
from ghostmap.models import ScanResult, SwitchInfo
from ghostmap.protocols.cip_tables import DEVICE_TYPE_MANAGED_SWITCH
from ghostmap.protocols.enip import ENIP_PORT
from ghostmap.protocols.snmp import PySnmpClient, SnmpClient, SnmpCredentials

SnmpFactory = Callable[[str, SnmpCredentials], SnmpClient]


@dataclass
class ScanRequest:
    targets: list[str] = field(default_factory=list)  # CIDRs / ranges / IPs for unicast ListIdentity
    broadcast: list[str] = field(default_factory=list)  # e.g. 192.168.1.255
    switches: list[str] = field(default_factory=list)  # switch IPs to read over SNMP
    snmp: Optional[SnmpCredentials] = None
    auto_switches: bool = True  # also SNMP any discovered CIP "Managed Ethernet Switch"
    timeout: float = 2.0
    rate: float = 200.0
    traffic_window: float = 10.0  # seconds between the two counter readings for port rates; 0 = skip
    enip_port: int = ENIP_PORT
    use_local_arp: bool = True
    baseline_path: Optional[str] = None
    label: str = ""

    def public_params(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "targets": self.targets,
            "broadcast": self.broadcast,
            "switches": self.switches,
            "snmp": self.snmp.redacted() if self.snmp else None,
            "auto_switches": self.auto_switches,
            "timeout": self.timeout,
            "rate": self.rate,
            "traffic_window": self.traffic_window,
            "baseline": self.baseline_path,
        }


def new_scan_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def run_scan(
    req: ScanRequest,
    *,
    log: Optional[Callable[[str], None]] = None,
    snmp_factory: SnmpFactory = PySnmpClient,
    arp_reader: Callable[[], dict[str, str]] = arp.read_arp_cache,
    scan_id: Optional[str] = None,
) -> ScanResult:
    result = ScanResult(id=scan_id or new_scan_id(), started=_now(), params=req.public_params())

    def say(msg: str) -> None:
        result.log.append(msg)
        if log:
            log(msg)

    baseline = load_baseline(req.baseline_path) if req.baseline_path else None  # fail fast on a bad path

    # 1. EtherNet/IP discovery
    hosts = discovery.expand_targets(req.targets) if req.targets else []
    if hosts or req.broadcast:
        say(f"ListIdentity sweep: {len(hosts)} unicast targets, {len(req.broadcast)} broadcast, {req.rate:g} pkt/s")
        last = [0]

        def progress(done: int, total: int) -> None:
            pct = done * 100 // total
            if pct >= last[0] + 25 or done == total:
                last[0] = pct
                say(f"  sent {done}/{total}")

        replies, errors = await discovery.discover(
            hosts, req.broadcast, port=req.enip_port, timeout=req.timeout, rate=req.rate, progress=progress
        )
        result.identities = replies
        for e in errors:
            say(f"  malformed reply: {e}")
        say(f"  {len({r.source_ip for r in replies})} EtherNet/IP devices answered")

    # 2. Stratix / managed switches over SNMP
    switch_ips = list(dict.fromkeys(req.switches))
    if req.auto_switches and req.snmp:
        for r in result.identities:
            if r.identity.device_type == DEVICE_TYPE_MANAGED_SWITCH and r.source_ip not in switch_ips:
                switch_ips.append(r.source_ip)
                say(f"  auto-adding switch {r.source_ip} ({r.identity.product_name})")
    if switch_ips and req.snmp is None:
        say("Switches given but no SNMP credentials - skipping switch collection")
        switch_ips = []

    async def one_switch(ip: str) -> SwitchInfo:
        client = snmp_factory(ip, req.snmp)
        try:
            say(f"SNMP: reading switch {ip}")
            sw = await portstats.sample_traffic(ip, client, lambda: stratix.collect_switch(ip, client), req.traffic_window)
            say(f"  {ip}: {sw.sys_name or '?'} {sw.model} IOS {sw.software_version} - {len(sw.ports)} interfaces"
                + (f", {len(sw.errors)} section errors" if sw.errors else ""))
            return sw
        finally:
            await client.close()

    if switch_ips:
        result.switches = list(await asyncio.gather(*(one_switch(ip) for ip in switch_ips)))

    # 3. Correlate
    # Only hosts we actually contacted: every swept host that answered ARP is a live
    # device on the machine network, even if it doesn't speak EtherNet/IP.
    local_arp = arp_reader() if req.use_local_arp else {}
    contacted = set(hosts) | {r.source_ip for r in result.identities}
    local_arp = {ip: mac for ip, mac in local_arp.items() if ip in contacted}
    non_cip = len(set(local_arp) - {r.source_ip for r in result.identities})
    if non_cip:
        say(f"  {non_cip} other hosts answered ARP (no EtherNet/IP reply)")
    result.devices = build_inventory(result.identities, result.switches, local_arp,
                                     local_ips=discovery.local_ips_for(hosts or req.broadcast))

    # 4. Diagnose
    result.findings = run_diagnostics(result.identities, result.devices, result.switches,
                                      baseline=baseline, thresholds=Thresholds())
    result.finished = _now()
    sev = {s: sum(1 for f in result.findings if f.severity == s) for s in ("error", "warning", "info")}
    say(f"Done: {len(result.devices)} devices, {len(result.switches)} switches, "
        f"{sev['error']} errors / {sev['warning']} warnings / {sev['info']} info")
    return result

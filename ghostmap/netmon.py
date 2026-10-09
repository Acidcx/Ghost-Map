"""Background traffic monitor: polls the machine's switches every ``interval_s`` seconds, whether or not anyone
is looking, and keeps what it saw in ``<data_dir>/network.db``:

- **rates**: per port and poll, bandwidth in and out (and % of link speed), broadcast and multicast packets
  per second, input errors and output drops per second; kept ``RATES_DAYS`` days;
- **cpu**: the switch CPU at each poll;
- **events**: a broadcast storm, a busy link, climbing errors, output drops or high CPU, from when a poll first
  saw it until a poll no longer does, with its peak; kept ``EVENTS_DAYS`` days.

A rate is the average over one poll interval, so a storm shorter than the interval shows up diluted. Reads are
SNMP GET/GETBULK only, about a dozen small walks per switch per poll. A switch named ``demo`` is the simulated one.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable, Optional

from ghostmap import secret
from ghostmap.analysis.diagnostics import Thresholds, _switch_health, traffic_findings
from ghostmap.analysis.traffic import traffic_between
from ghostmap.collectors.portstats import read_counters
from ghostmap.collectors.stratix import collect_layout, read_cpu
from ghostmap.models import Port, SwitchHealth, SwitchInfo
from ghostmap.protocols.snmp import PySnmpClient, SnmpClient, SnmpCredentials

log = logging.getLogger("ghostmap.netmon")
CONFIG_NAME, DB_NAME = "netmon.json", "network.db"
CONFIG_VERSION = 1
SCHEMA_VERSION = 1
RATES_DAYS = 7
EVENTS_DAYS = 90
LAYOUT_EVERY_S = 600.0  # names, link speeds and uplinks are re-read this often
PRUNE_EVERY_S = 3600.0
SECRETS = ("community", "auth_key", "priv_key")
SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}

# Finding code -> the PortTraffic value its event's peak is measured by, and the unit shown
PEAK_OF = {
    "port.utilization": (lambda tr: max(tr.in_util or 0, tr.out_util or 0), "%"),
    "port.broadcast_storm": (lambda tr: tr.in_bcast_pps or 0, "pkt/s"),
    "port.multicast_high": (lambda tr: tr.in_mcast_pps or 0, "pkt/s"),
    "port.multicast_flood": (lambda tr: tr.out_mcast_pps or 0, "pkt/s"),
    "port.errors_rising": (lambda tr: tr.in_errors_ps or 0, "/s"),
    "port.drops": (lambda tr: tr.out_discards_ps or 0, "pkt/s"),
}


@dataclass
class NetmonConfig:
    switches: list[str] = field(default_factory=list)  # IPs or host names; "demo" = the simulated switch
    interval_s: int = 30
    version: str = "2c"
    community: str = field(default="", repr=False)
    username: str = ""
    auth_protocol: str = "sha"
    auth_key: str = field(default="", repr=False)
    priv_protocol: str = "aes"
    priv_key: str = field(default="", repr=False)
    port: int = 161
    timeout: float = 2.0

    def check(self) -> None:
        self.switches = list(dict.fromkeys(s.strip() for s in self.switches if s.strip()))
        if not self.switches:
            raise ValueError("give at least one switch IP")
        if len(self.switches) > 32:
            raise ValueError("at most 32 switches")
        if not 10 <= int(self.interval_s) <= 3600:
            raise ValueError("poll interval must be 10 to 3600 seconds")
        if self.version not in ("2c", "3"):
            raise ValueError("SNMP version must be 2c or 3")
        real = [s for s in self.switches if s.lower() != "demo"]
        if real and self.version == "2c" and not self.community:
            raise ValueError("a read-only community is required for SNMP v2c")
        if real and self.version == "3" and not self.username:
            raise ValueError("a user name is required for SNMP v3")

    def creds(self) -> SnmpCredentials:
        return SnmpCredentials(version=self.version, community=self.community, username=self.username,
                               auth_protocol=self.auth_protocol, auth_key=self.auth_key, priv_protocol=self.priv_protocol,
                               priv_key=self.priv_key, port=self.port, timeout=self.timeout)


SCHEMA = """
CREATE TABLE IF NOT EXISTS rates (
    switch TEXT NOT NULL, if_index INTEGER NOT NULL, port TEXT, at REAL NOT NULL, seconds REAL,
    in_bps REAL, out_bps REAL, in_util REAL, out_util REAL, in_bcast REAL, out_bcast REAL,
    in_mcast REAL, out_mcast REAL, in_err REAL, out_drop REAL);
CREATE INDEX IF NOT EXISTS rates_switch_at ON rates (switch, at);
CREATE INDEX IF NOT EXISTS rates_port_at ON rates (switch, port, at);
CREATE TABLE IF NOT EXISTS cpu (switch TEXT NOT NULL, at REAL NOT NULL, cpu_5s INTEGER, cpu_1m INTEGER);
CREATE INDEX IF NOT EXISTS cpu_switch_at ON cpu (switch, at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, switch TEXT NOT NULL, name TEXT, port TEXT NOT NULL, code TEXT NOT NULL,
    severity TEXT NOT NULL, message TEXT, hint TEXT, start REAL NOT NULL, last REAL NOT NULL, end REAL,
    peak REAL, unit TEXT);
CREATE INDEX IF NOT EXISTS events_start ON events (start);
"""

RATE_COLS = ("in_bps", "out_bps", "in_util", "out_util", "in_bcast", "out_bcast", "in_mcast", "out_mcast", "in_err", "out_drop")
_TRAFFIC_ATTR = {"in_bcast": "in_bcast_pps", "out_bcast": "out_bcast_pps", "in_mcast": "in_mcast_pps",
                 "out_mcast": "out_mcast_pps", "in_err": "in_errors_ps", "out_drop": "out_discards_ps"}


class TrafficStore:
    """``network.db``. The monitor writes on the event loop; API reads run in worker threads."""

    def __init__(self, root: Path):
        self.path = Path(root) / DB_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            if self._db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            # Events left open by the last run end when they were last seen.
            self._db.execute("UPDATE events SET end = last WHERE end IS NULL")
            self._db.commit()
        self._pruned = 0.0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def write(self, switch: str, at: float, rows: list[tuple], cpu: Optional[tuple]) -> None:
        with self._lock:
            self._db.executemany(f"INSERT INTO rates (switch, if_index, port, at, seconds, {', '.join(RATE_COLS)}) "
                                 f"VALUES (?, ?, ?, ?, ?{', ?' * len(RATE_COLS)})", rows)
            if cpu is not None:
                self._db.execute("INSERT INTO cpu (switch, at, cpu_5s, cpu_1m) VALUES (?, ?, ?, ?)", (switch, at, *cpu))
            if at - self._pruned >= PRUNE_EVERY_S:
                self._db.execute("DELETE FROM rates WHERE at < ?", (at - RATES_DAYS * 86400,))
                self._db.execute("DELETE FROM cpu WHERE at < ?", (at - RATES_DAYS * 86400,))
                self._db.execute("DELETE FROM events WHERE start < ? AND end IS NOT NULL", (at - EVENTS_DAYS * 86400,))
                self._pruned = at
            self._db.commit()

    def event_open(self, switch: str, name: str, port: str, code: str, severity: str, message: str, hint: str,
                   at: float, peak: Optional[float], unit: str) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO events (switch, name, port, code, severity, message, hint, start, last, "
                                   "peak, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (switch, name, port, code, severity, message, hint, at, at, peak, unit))
            self._db.commit()
            return cur.lastrowid

    def event_update(self, eid: int, at: float, severity: str, message: Optional[str], peak: Optional[float]) -> None:
        with self._lock:
            if message is not None:  # a new peak: keep the message that describes it
                self._db.execute("UPDATE events SET last = ?, severity = ?, message = ?, peak = ? WHERE id = ?",
                                 (at, severity, message, peak, eid))
            else:
                self._db.execute("UPDATE events SET last = ?, severity = ? WHERE id = ?", (at, severity, eid))
            self._db.commit()

    def event_close(self, eid: int, at: float) -> None:
        with self._lock:
            self._db.execute("UPDATE events SET end = ?, last = ? WHERE id = ?", (at, at, eid))
            self._db.commit()

    def forget(self, switch: str) -> None:
        with self._lock:
            for t in ("rates", "cpu", "events"):
                self._db.execute(f"DELETE FROM {t} WHERE switch = ?", (switch,))
            self._db.commit()

    # ----------------------------------------------------------------- reads
    def _rows(self, sql: str, args: tuple) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def peaks(self, switch: str, since: float) -> dict[str, dict]:
        rows = self._rows("SELECT port, MAX(MAX(COALESCE(in_util, 0)), MAX(COALESCE(out_util, 0))) AS util, "
                          "MAX(in_bcast) AS in_bcast, MAX(in_mcast) AS in_mcast, MAX(in_err) AS in_err, "
                          "MAX(out_drop) AS out_drop FROM rates WHERE switch = ? AND at >= ? GROUP BY port",
                          (switch, since))
        return {r.pop("port"): r for r in rows}

    def series(self, switch: str, port: str, since: float, until: float, points: int = 360) -> list[dict]:
        """Averages per time bucket for bandwidth, maxima for packets per second (storms are short)."""
        b = max(1.0, (until - since) / points)
        return self._rows(
            "SELECT CAST(at / ? AS INTEGER) * ? AS t, AVG(in_bps) AS in_bps, AVG(out_bps) AS out_bps, "
            "MAX(in_util) AS in_util, MAX(out_util) AS out_util, MAX(in_bcast) AS in_bcast, MAX(out_bcast) AS out_bcast, "
            "MAX(in_mcast) AS in_mcast, MAX(out_mcast) AS out_mcast, MAX(in_err) AS in_err, MAX(out_drop) AS out_drop "
            "FROM rates WHERE switch = ? AND port = ? AND at >= ? AND at <= ? GROUP BY t ORDER BY t",
            (b, b, switch, port, since, until))

    def cpu_series(self, switch: str, since: float, until: float, points: int = 360) -> list[dict]:
        b = max(1.0, (until - since) / points)
        return self._rows("SELECT CAST(at / ? AS INTEGER) * ? AS t, MAX(cpu_5s) AS cpu_5s, AVG(cpu_1m) AS cpu_1m "
                          "FROM cpu WHERE switch = ? AND at >= ? AND at <= ? GROUP BY t ORDER BY t",
                          (b, b, switch, since, until))

    def events(self, since: float, switch: Optional[str] = None, limit: int = 500) -> list[dict]:
        if switch:
            return self._rows("SELECT * FROM events WHERE (start >= ? OR end IS NULL OR end >= ?) AND switch = ? "
                              "ORDER BY start DESC LIMIT ?", (since, since, switch, limit))
        return self._rows("SELECT * FROM events WHERE start >= ? OR end IS NULL OR end >= ? ORDER BY start DESC LIMIT ?",
                          (since, since, limit))

    def events_csv(self, since: float) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["start", "end", "seconds", "switch", "port", "severity", "code", "peak", "unit", "message"])

        def fmt(t):
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
        for e in reversed(self.events(since, limit=100000)):
            end = e["end"] or e["last"]
            w.writerow([fmt(e["start"]), fmt(e["end"]), round(end - e["start"]), e["name"] or e["switch"], e["port"],
                        e["severity"], e["code"], e["peak"], e["unit"], e["message"]])
        return out.getvalue()

    def counts(self) -> dict:
        with self._lock:
            n = {t: self._db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("rates", "cpu", "events")}
        n["bytes"] = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.is_file())
        return n


def _error_text(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class NetMonitor:
    """The saved settings plus one polling loop per switch."""

    def __init__(self, root: Path, demo: bool = False, snmp_factory: Optional[Callable] = None,
                 thresholds: Optional[Thresholds] = None, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic):
        """``clock``: wall time for what is stored; ``mono``: for rates (tests pass simulated time to both)."""
        self.root = Path(root)
        self.path = self.root / CONFIG_NAME
        self.store = TrafficStore(self.root)
        self.thresholds = thresholds or Thresholds()
        self.snmp_factory = snmp_factory or PySnmpClient
        self.clock, self.mono = clock, mono
        self.cfg: Optional[NetmonConfig] = None
        self.load_error: Optional[str] = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._st: dict[str, dict] = {}
        self._super: Optional[asyncio.Task] = None
        if self.path.exists():
            try:
                self.cfg = self._read()
            except Exception as exc:
                self.load_error = _error_text(exc)
                log.warning("can't load %s: %s", self.path, self.load_error)
        elif demo:
            self.cfg = NetmonConfig(switches=["demo"], interval_s=10)

    # ------------------------------------------------------------- config
    def _read(self) -> NetmonConfig:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(NetmonConfig)} - set(SECRETS)
        cfg = NetmonConfig(**{k: v for k, v in raw.items() if k in known})
        for k in SECRETS:
            setattr(cfg, k, secret.unprotect(raw.get(f"{k}_protected", ""), self.root))
        return cfg

    def public(self) -> dict:
        c = self.cfg or NetmonConfig()
        out = {k: v for k, v in asdict(c).items() if k not in SECRETS}
        return {**out, **{f"has_{k}": bool(getattr(c, k)) for k in SECRETS}, "configured": self.cfg is not None,
                "load_error": self.load_error}

    def merged(self, data: dict) -> NetmonConfig:
        """New settings from the form; a blank secret keeps the saved one."""
        known = {f.name for f in fields(NetmonConfig)}
        cfg = NetmonConfig(**{k: v for k, v in data.items() if k in known})
        for k in SECRETS:
            if not getattr(cfg, k) and self.cfg:
                setattr(cfg, k, getattr(self.cfg, k))
        cfg.check()
        return cfg

    def save(self, cfg: NetmonConfig) -> None:
        data = {k: v for k, v in asdict(cfg).items() if k not in SECRETS}
        for k in SECRETS:
            data[f"{k}_protected"] = secret.protect(getattr(cfg, k), self.root)
        data["config_version"] = CONFIG_VERSION
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"netmon.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        old = set(self.cfg.switches) if self.cfg else set()
        self.cfg, self.load_error = cfg, None
        for ip in old - set(cfg.switches):
            self._stop_one(ip)
        for ip in set(cfg.switches) & old:
            self._stop_one(ip)  # restarted with the new credentials and interval by the supervisor

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        if self.cfg:
            for ip in list(self._tasks):
                self._stop_one(ip)
        self.cfg, self.load_error = None, None

    # ------------------------------------------------------------- polling
    def client_for(self, ip: str, cfg: NetmonConfig) -> SnmpClient:
        if ip.lower() == "demo":
            from ghostmap.sim.machine import demo_switch_client

            return demo_switch_client("today")
        return self.snmp_factory(ip, cfg.creds())

    def start(self) -> None:
        if self._super is None:
            self._super = asyncio.create_task(self._supervise(), name="ghostmap-netmon")

    async def stop(self) -> None:
        tasks = [t for t in [self._super, *self._tasks.values()] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._super, self._tasks = None, {}
        self.store.close()

    def _stop_one(self, ip: str) -> None:
        t = self._tasks.pop(ip, None)
        if t:
            t.cancel()
        st = self._st.pop(ip, None)
        if st:
            for eid, *_ in st["open"].values():
                self.store.event_close(eid, st.get("last_ok") or self.clock())

    async def _supervise(self) -> None:
        while True:
            cfg = self.cfg
            for ip in (cfg.switches if cfg else []):
                t = self._tasks.get(ip)
                if t is None or t.done():
                    self._tasks[ip] = asyncio.create_task(self._loop(ip, cfg), name=f"ghostmap-netmon-{ip}")
            await asyncio.sleep(2.0)

    async def _loop(self, ip: str, cfg: NetmonConfig) -> None:
        st = self._st.setdefault(ip, {"open": {}, "prev": None, "layout": None, "layout_at": 0.0, "last_ok": None,
                                      "error": None, "polls": 0, "latest": {}, "cpu": None})
        client = self.client_for(ip, cfg)
        try:
            while True:
                t0 = time.monotonic()
                try:
                    await self.poll_once(ip, client, st)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    st["error"], st["error_at"] = _error_text(exc), self.clock()
                    st["prev"] = None  # don't compute a rate across the gap
                    log.info("traffic monitor: %s poll failed: %s", ip, st["error"])
                await asyncio.sleep(max(1.0, cfg.interval_s - (time.monotonic() - t0)))
        finally:
            await client.close()

    async def poll_once(self, ip: str, client: SnmpClient, st: dict) -> None:
        now = self.clock()
        if st["layout"] is None or now - st["layout_at"] >= LAYOUT_EVERY_S:
            sw = await collect_layout(ip, client)
            st["layout"], st["layout_at"] = sw, now
        layout: SwitchInfo = st["layout"]
        sample = await read_counters(client, self.mono)
        cpu_sw = SwitchInfo(ip=ip)
        try:
            await read_cpu(cpu_sw, client)
        except Exception:
            log.debug("traffic monitor: %s cpu read failed", ip, exc_info=True)
        prev, st["prev"] = st["prev"], sample
        st["last_ok"], st["error"], st["polls"] = now, None, st["polls"] + 1
        h = cpu_sw.health
        st["cpu"] = None if h.cpu_5s is None else {"cpu_5s": h.cpu_5s, "cpu_1m": h.cpu_1m, "cpu_5m": h.cpu_5m}
        if prev is None:
            return
        rates = traffic_between(prev, sample)
        name = layout.sys_name or ip
        rows, latest, found = [], {}, {}
        for p in layout.ports:
            tr = rates.get(p.if_index)
            if tr is None or not p.is_physical:
                continue
            port = Port(if_index=p.if_index, name=p.name, alias=p.alias, speed_mbps=sample.speed_mbps.get(p.if_index, p.speed_mbps),
                        is_uplink=p.is_uplink, traffic=tr, if_type=p.if_type)
            vals = (tr.in_bps, tr.out_bps, tr.in_util, tr.out_util, *(getattr(tr, _TRAFFIC_ATTR[c]) for c in RATE_COLS[4:]))
            latest[p.name] = {"if_index": p.if_index, "port": p.name, "alias": p.alias, "speed_mbps": port.speed_mbps,
                              "uplink": p.is_uplink, "oper": p.oper_status, "seconds": tr.seconds, **dict(zip(RATE_COLS, vals))}
            if any(vals):  # idle and down ports cost no rows
                rows.append((ip, p.if_index, p.name, now, tr.seconds, *vals))
            for f in traffic_findings(f"{name} {p.name}", port, self.thresholds):
                found[(p.name, f.code)] = (f, PEAK_OF[f.code][0](tr), PEAK_OF[f.code][1])
        for f in _switch_health(SwitchInfo(ip=ip, uptime_seconds=layout.uptime_seconds, health=SwitchHealth(
                cpu_5s=h.cpu_5s, cpu_1m=h.cpu_1m, cpu_5m=h.cpu_5m)), name, self.thresholds):
            if f.code == "switch.cpu_high":  # a 5 s spike between two polls isn't worth an event
                found[("", f.code)] = (f, h.cpu_5m, "%")
        self.store.write(ip, now, rows, (h.cpu_5s, h.cpu_1m) if h.cpu_5s is not None else None)
        st["latest"], st["latest_at"] = latest, now
        self._update_events(ip, name, st, found, now)

    def _update_events(self, ip: str, name: str, st: dict, found: dict, now: float) -> None:
        opened = st["open"]
        for key, (f, peak, unit) in found.items():
            if key in opened:
                eid, old_peak, old_sev = opened[key]
                sev = min(old_sev, f.severity, key=SEVERITY_RANK.get)
                new_peak = peak is not None and (old_peak is None or peak > old_peak)
                self.store.event_update(eid, now, sev, f.message if new_peak else None, peak if new_peak else old_peak)
                opened[key] = (eid, peak if new_peak else old_peak, sev)
            else:
                eid = self.store.event_open(ip, name, key[0], f.code, f.severity, f.message, f.hint, now, peak, unit)
                opened[key] = (eid, peak, f.severity)
                log.info("traffic monitor: %s %s %s started: %s", name, key[0], f.code, f.message)
        for key in [k for k in opened if k not in found]:
            eid, *_ = opened.pop(key)
            self.store.event_close(eid, now)

    async def seed_demo(self, hours: float = 6.0, step: float = 30.0) -> None:
        """Demo mode: fill an empty database with ``hours`` of the simulated switch, so the charts and events
        have something to show right away."""
        if self.store.counts()["rates"]:
            return
        from ghostmap.sim.machine import demo_switch_client

        end = self.clock()
        t = [end - hours * 3600]
        client = demo_switch_client("today", clock=lambda: t[0] - end + hours * 3600)
        saved = self.clock, self.mono
        self.clock = self.mono = lambda: t[0]
        st = {"open": {}, "prev": None, "layout": None, "layout_at": 0.0, "last_ok": None, "error": None, "polls": 0,
              "latest": {}, "cpu": None}
        try:
            while t[0] < end - step:
                await self.poll_once("demo", client, st)
                t[0] += step
            for eid, *_ in st["open"].values():
                self.store.event_close(eid, t[0])
        finally:
            self.clock, self.mono = saved

    # ------------------------------------------------------------- status
    def status(self) -> dict:
        cfg = self.cfg
        out = []
        for ip in (cfg.switches if cfg else []):
            st = self._st.get(ip) or {}
            layout = st.get("layout")
            out.append({"switch": ip, "name": layout.sys_name if layout else None,
                        "model": layout.model if layout else None, "last_ok": st.get("last_ok"),
                        "error": st.get("error"), "error_at": st.get("error_at"), "polls": st.get("polls", 0),
                        "cpu": st.get("cpu"), "open_events": len(st.get("open") or {})})
        t = self.thresholds
        return {"configured": cfg is not None, "interval_s": cfg.interval_s if cfg else None,
                "thresholds": {"util_percent": t.util_percent, "bcast_pps": t.bcast_pps, "bcast_storm_pps": t.bcast_storm_pps,
                               "mcast_pps": t.mcast_pps, "errors_per_s": t.errors_per_s, "drops_per_s": t.drops_per_s},
                "running": self._super is not None, "switches": out, "load_error": self.load_error}

    def now(self, ip: str, hours: float = 24) -> dict:
        st = self._st.get(ip) or {}
        since = self.clock() - hours * 3600
        return {"switch": ip, "at": st.get("latest_at"), "cpu": st.get("cpu"),
                "ports": list((st.get("latest") or {}).values()), "peaks": self.store.peaks(ip, since),
                "open": [{"port": k[0], "code": k[1]} for k in (st.get("open") or {})]}

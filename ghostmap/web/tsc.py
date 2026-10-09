"""The TSC link for the web app: the saved connection, a short cache, and the status shown in the UI.

Queries run in a worker thread (python-tds is blocking) and each answer is reused for ``cache_s`` seconds, so
any number of people watching the Production page cost the SQL Server one small query per line per period.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ghostmap import secret
from ghostmap.analysis.production import FEET_PER, HOURS_SHOWN, current_shift, shift_start, summary
from ghostmap.collectors.tsc import FEATURES, TscConfig, parse_shifts, source_for

log = logging.getLogger("ghostmap.tsc")
CONFIG_VERSION = 1
META_S = 600       # stations, links and the shift calendar change rarely
COUNTERS_S = 300   # maintenance counters: sums over days of parts, no need to be fresher
ALL_TIME = datetime(1901, 1, 2)
WINDOWS = (("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30)))


def error_text(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class TscService:
    def __init__(self, root: Path, demo: bool = False):
        self.path = Path(root) / "tsc.json"
        self.root = Path(root)
        self.cfg: Optional[TscConfig] = None
        self._cache: dict = {}
        self._locks: dict = {}
        self.state = {"last_ok": None, "last_error": None, "last_error_at": None, "last_ms": None, "queries": 0}
        self.features: dict = {}  # optional reads: name -> {ok, error, at}
        self._meta: dict = {}     # name -> (monotonic time, rows or None)
        self.load_error: Optional[str] = None
        if self.path.exists():
            try:
                self.cfg = self._read()
            except Exception as exc:  # a bad or foreign config: show why and let the admin enter it again
                self.load_error = error_text(exc)
                log.warning("can't load %s: %s", self.path, self.load_error)
        elif demo:
            self.cfg = TscConfig(server="demo")

    # ------------------------------------------------------------- config
    def _read(self) -> TscConfig:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(TscConfig)} - {"password"}
        cfg = TscConfig(**{k: v for k, v in raw.items() if k in known})
        cfg.password = secret.unprotect(raw.get("password_protected", ""), self.root)
        return cfg

    def public(self) -> dict:
        """The settings without the password (never sent back to a browser)."""
        c = self.cfg or TscConfig()
        out = {k: v for k, v in asdict(c).items() if k != "password"}
        return {**out, "configured": self.cfg is not None, "has_password": bool(c.password),
                "demo": c.server.strip().lower() == "demo", "load_error": self.load_error}

    def merged(self, data: dict) -> TscConfig:
        """New settings from the form; a blank password keeps the saved one."""
        known = {f.name for f in fields(TscConfig)}
        cfg = TscConfig(**{k: v for k, v in data.items() if k in known})
        if not cfg.password and self.cfg:
            cfg.password = self.cfg.password
        cfg.server, cfg.database, cfg.username = cfg.server.strip(), cfg.database.strip(), cfg.username.strip()
        cfg.view, cfg.queue_view, cfg.done_view = cfg.view.strip(), cfg.queue_view.strip(), cfg.done_view.strip()
        cfg.check()
        return cfg

    def save(self, cfg: TscConfig) -> None:
        data = {k: v for k, v in asdict(cfg).items() if k != "password"}
        data["password_protected"] = secret.protect(cfg.password, self.root)
        data["config_version"] = CONFIG_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"tsc.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        self.cfg, self.load_error, self._cache, self._meta, self.features = cfg, None, {}, {}, {}

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        self.cfg, self.load_error, self._cache, self._meta, self.features = None, None, {}, {}, {}

    # ------------------------------------------------------------- reads
    async def _run(self, key, fn, max_age: float):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < max_age:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
            t0 = time.perf_counter()
            try:
                value = await asyncio.to_thread(fn)
            except Exception as exc:
                self.state.update(last_error=error_text(exc), last_error_at=time.time())
                log.warning("TSC query failed: %s", self.state["last_error"])
                raise
            self.state.update(last_ok=time.time(), last_error=None, last_ms=round((time.perf_counter() - t0) * 1000),
                              queries=self.state["queries"] + 1)
            self._cache[key] = (time.monotonic(), value)
            return value

    def _need(self) -> TscConfig:
        if self.cfg is None:
            raise LookupError("no TSC connection is set up yet (Production > Connection)")
        return self.cfg

    async def lines(self) -> list[dict]:
        cfg = self._need()
        src = source_for(cfg)
        rows = await self._run("lines", src.lines, max(cfg.cache_s, 60))
        return [{"id": int(r["LineID"]), "name": str(r.get("LineName") or f"Line {r['LineID']}")}
                for r in rows if r.get("LineID") is not None]

    def _try(self, feature: str, fn, max_age: float = 0):
        """An optional read: its rows, or None when the login may not read it (the page works without it).
        Runs in the worker thread. ``max_age``: reuse the last answer this long (lookups that rarely change)."""
        hit = self._meta.get(feature)
        if max_age and hit and time.monotonic() - hit[0] < max_age:
            return hit[1]
        try:
            rows = fn()
        except Exception as exc:
            err = error_text(exc)
            if (self.features.get(feature) or {}).get("error") != err:
                log.info("TSC optional read %s not available: %s", feature, err)
            self.features[feature] = {"ok": False, "error": err, "at": time.time()}
            rows = None
        else:
            self.features[feature] = {"ok": True, "error": None, "at": time.time()}
        if max_age:
            self._meta[feature] = (time.monotonic(), rows)
        return rows

    def _shift(self, src, now: datetime, cfg: TscConfig) -> dict:
        """The current shift from TSC's calendar, else from the start times in the settings."""
        rows = self._try("shifts", src.shifts, META_S)
        cur = current_shift(now, rows) if rows else None
        if cur:
            return {**cur, "source": "tsc"}
        starts = parse_shifts(cfg.shifts)
        start = shift_start(now, starts)
        nxt = [datetime.combine(start.date() + timedelta(days=d), datetime.min.time()).replace(hour=h, minute=m)
               for d in (0, 1) for h, m in starts]
        return {"name": "", "start": start, "end": min(t for t in nxt if t > start), "source": "settings"}

    async def production(self, line_id: int) -> dict:
        cfg = self._need()
        src = source_for(cfg)

        def fetch():
            now = datetime.now().replace(microsecond=0)
            shift = self._shift(src, now, cfg)
            since = min(shift["start"], now.replace(minute=0, second=0) - timedelta(hours=HOURS_SHOWN - 1))
            open_rows, done_rows = src.open_parts(line_id), src.done_parts(line_id, since)
            orders = sorted({str(r["OrderNumber"]) for r in open_rows if r.get("OrderNumber") is not None})
            totals = src.order_totals(line_id, orders)
            stations = self._try("stations", src.stations, META_S)
            stations = [s for s in stations if s.get("LineID") in (None, line_id)] if stations else None
            station_parts = None
            if stations:
                station_parts = self._try("stations", lambda: src.station_parts([str(r["PartID"]) for r in open_rows]))
            links = self._try("station_links", src.station_links, META_S) if stations else None
            stops = self._try("downtime", lambda: src.downtime(line_id, shift["start"]))
            return summary(open_rows, done_rows, now, shift, cfg.length_unit, stations, station_parts, links, stops, totals)

        out = await self._run(("line", line_id), fetch, cfg.cache_s)
        return {"line": line_id, "source": src.name, **out}

    async def counters(self, line_id: int) -> dict:
        """Production counters for maintenance: feet, pieces, production hours and punch strokes over the last
        day, week, month and all time (everything in the completed parts view)."""
        cfg = self._need()
        src = source_for(cfg)
        k = FEET_PER.get(cfg.length_unit, 1 / 12)

        def fetch():
            now = datetime.now().replace(microsecond=0)
            periods = {}
            for name, since in [*((n, now - d) for n, d in WINDOWS), ("all", ALL_TIME)]:
                t = src.totals(line_id, since) or {}
                periods[name] = {"parts": int(t.get("Parts") or 0), "pieces": int(t.get("Pieces") or 0),
                                 "feet": round(float(t.get("LengthTotal") or 0) * k, 1),
                                 "scrap_pieces": int(t.get("ScrapPieces") or 0),
                                 "prod_hours": round(float(t.get("RunSeconds") or 0) / 3600, 1),
                                 "first": t.get("FirstEnd").timestamp() if isinstance(t.get("FirstEnd"), datetime) else None}
            tools = {str(r["HoleType"]): str(r.get("Description") or "") for r in self._try("tool_types", src.tool_types, META_S) or []}
            line_stations = [s for s in self._try("stations", src.stations, META_S) or [] if s.get("LineID") in (None, line_id)]
            names = {int(s["StationID"]): str(s.get("Name") or "") for s in line_stations}
            strokes: dict = {}
            for name, since in (("30d", now - timedelta(days=30)), ("all", ALL_TIME)):
                for r in self._try("strokes", lambda since=since: src.strokes(line_id, since)) or []:
                    sid, tool = int(r.get("StationID") or 0), str(r.get("ToolType") or "")
                    x = strokes.setdefault((sid, tool), {"station": sid, "station_name": names.get(sid) or f"Station {sid}",
                                                         "tool": tool, "tool_name": tools.get(tool, ""), "30d": 0, "all": 0})
                    x[name] = int(r.get("Strokes") or 0)
            return {"at": now.timestamp(), "unit": cfg.length_unit, "periods": periods,
                    "stations": [{"id": k, "name": v or f"Station {k}"} for k, v in sorted(names.items())],
                    "tools": [{"tool": k, "name": v} for k, v in sorted(tools.items())],
                    "strokes": sorted(strokes.values(), key=lambda x: (x["station"], -x["all"])),
                    "strokes_ok": (self.features.get("strokes") or {}).get("ok", False)}

        return {"line": line_id, "source": src.name, **await self._run(("counters", line_id), fetch, max(cfg.cache_s, COUNTERS_S))}

    def since_values(self, line_id: int, since: datetime) -> dict:
        """Feet, pieces, production hours and strokes per (station, tool) since a time (blocking; for service
        items, cached by the caller)."""
        cfg = self._need()
        src = source_for(cfg)
        t = src.totals(line_id, since) or {}
        strokes = {}
        for r in self._try("strokes", lambda: src.strokes(line_id, since)) or []:
            strokes[(int(r.get("StationID") or 0), str(r.get("ToolType") or ""))] = int(r.get("Strokes") or 0)
        return {"feet": float(t.get("LengthTotal") or 0) * FEET_PER.get(cfg.length_unit, 1 / 12),
                "pieces": float(t.get("Pieces") or 0), "prod_hours": float(t.get("RunSeconds") or 0) / 3600,
                "strokes": strokes}

    async def values_since(self, line_id: int, since: float) -> dict:
        cfg = self._need()
        key = ("since", line_id, int(since))
        return await self._run(key, lambda: self.since_values(line_id, datetime.fromtimestamp(since)), max(cfg.cache_s, COUNTERS_S))

    def status(self) -> dict:
        c = self.cfg
        feats = {name: {"description": d, "grant": g.replace("<login>", (c.username if c and c.username else "<login>")),
                        **(self.features.get(name) or {"ok": None, "error": None, "at": None})}
                 for name, (d, g) in FEATURES.items()}
        return {"configured": c is not None, "source": None if c is None else ("demo" if c.server.lower() == "demo" else "sql"),
                "server": c.server if c else None, "database": c.database if c else None,
                "view": c.view if c else None, "queue_view": c.queue_view if c else None, "done_view": c.done_view if c else None,
                "features": feats, "load_error": self.load_error, **self.state}

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
from ghostmap.analysis.production import HOURS_SHOWN, shift_start, summary
from ghostmap.collectors.tsc import TscConfig, parse_shifts, source_for

log = logging.getLogger("ghostmap.tsc")
CONFIG_VERSION = 1


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
        cfg.server, cfg.database, cfg.username, cfg.view = (cfg.server.strip(), cfg.database.strip(),
                                                            cfg.username.strip(), cfg.view.strip())
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
        self.cfg, self.load_error, self._cache = cfg, None, {}

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        self.cfg, self.load_error, self._cache = None, None, {}

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

    async def production(self, line_id: int) -> dict:
        cfg = self._need()
        src = source_for(cfg)
        shifts = parse_shifts(cfg.shifts)

        def fetch():
            now = datetime.now().replace(microsecond=0)
            since = min(shift_start(now, shifts), now.replace(minute=0, second=0) - timedelta(hours=HOURS_SHOWN - 1))
            return now, src.open_parts(line_id), src.done_parts(line_id, since)

        now, open_rows, done_rows = await self._run(("line", line_id), fetch, cfg.cache_s)
        return {"line": line_id, "source": src.name, **summary(open_rows, done_rows, now, shifts, cfg.length_unit)}

    def status(self) -> dict:
        c = self.cfg
        return {"configured": c is not None, "source": None if c is None else ("demo" if c.server.lower() == "demo" else "sql"),
                "server": c.server if c else None, "database": c.database if c else None,
                "view": c.view if c else None, "load_error": self.load_error, **self.state}

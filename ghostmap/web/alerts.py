"""The alert list: every ``EVAL_S`` the rules in ``analysis/alerts.py`` run over what each part of Ghost Map knows.

Each alert keeps the time it started for as long as it lasts. A new alert, or one that gets worse, is written to
Ghost Map's log and the Windows Application event log (source "GhostMap"); so is an alert that clears. The web
UI shows the list as a banner (``GET /api/alerts``).

An admin can take an alert off the banner without fixing it (``mute``), which is what keeps the list worth reading
during commissioning and on a PC with a known, accepted problem:

- **acknowledge**: until it clears, or gets worse;
- **mute** for a while: one alert, or every alert with the same code (all ``machine.stopped`` while the machine is
  still being commissioned).

Muted alerts stay in the view (under ``muted``) and in the log and event log, so nothing is hidden for good; they
just don't count. Mutes are kept in ``alerts-muted.json`` so a restart doesn't bring the noise back.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from ghostmap import winevent

EVAL_S = 30.0
RECENT_KEPT = 200
FRESH_S = 10.0   # a page asking for the list gets one at most this old
log = logging.getLogger("ghostmap.alerts")
RANK = {"error": 0, "warning": 1}
MUTES_FILE = "alerts-muted.json"
ACK_GONE_S = 600            # an acknowledged alert that has been gone this long has cleared: the next one is news
MAX_MUTE_S = 30 * 86400     # a mute always ends; a month covers any commissioning phase


class AlertCenter:
    def __init__(self, sources: dict[str, Callable], eventlog: Optional[Callable[[int, int, str], bool]] = None,
                 data_dir: Optional[Path] = None):
        """``sources``: name -> fn(now) returning alerts (or a coroutine of them). ``data_dir``: where mutes are
        kept (none: in memory only)."""
        self.sources = sources
        self.mutes_path = Path(data_dir) / MUTES_FILE if data_dir else None
        self.mutes: dict[str, dict] = self._load_mutes()
        self.eventlog = eventlog if eventlog is not None else winevent.write
        self.active: dict[str, dict] = {}
        self.recent: deque = deque(maxlen=RECENT_KEPT)
        self.errors: dict[str, str] = {}
        self.evaluated_at: Optional[float] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="ghostmap-alerts")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(EVAL_S)  # give the collector and monitors a first round before judging them
        while True:
            try:
                await self.evaluate()
            except Exception:
                log.exception("alert evaluation failed")
            await asyncio.sleep(EVAL_S)

    async def current(self) -> dict:
        if self.evaluated_at is None or time.time() - self.evaluated_at > FRESH_S:
            await self.evaluate()
        return self.view()

    async def evaluate(self, now: Optional[float] = None) -> list[dict]:
        async with self._lock:
            now = time.time() if now is None else now
            found: dict[str, dict] = {}
            for name, src in self.sources.items():
                try:
                    got = src(now)
                    if inspect.isawaitable(got):
                        got = await got
                    self.errors.pop(name, None)
                except Exception as exc:  # a broken source keeps its alerts rather than clearing them
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                    log.warning("alerts: %s failed: %s", name, self.errors[name])
                    got = [a for a in self.active.values() if a["source"] == name]
                for a in got:
                    found[a["key"]] = {**a, "source": name}
            for key, a in found.items():
                old = self.active.get(key)
                a["since"] = old["since"] if old else (a.get("at") or now)
                if old is None or RANK.get(a["severity"], 9) < RANK.get(old["severity"], 9):
                    self._report("raised", a, now)
            for key in [k for k in self.active if k not in found]:
                self._report("cleared", self.active[key], now)
            self.active = found
            self.evaluated_at = now
            self._expire_mutes(now)
            return self.list()

    # ------------------------------------------------------------------------------ acknowledge and mute
    def _load_mutes(self) -> dict[str, dict]:
        if not self.mutes_path:
            return {}
        try:
            got = json.loads(self.mutes_path.read_text(encoding="utf-8"))
            return {m["target"]: m for m in got.get("mutes", []) if m.get("target")}
        except (OSError, ValueError, AttributeError, TypeError, KeyError):
            return {}

    def _save_mutes(self) -> None:
        if not self.mutes_path:
            return
        tmp = self.mutes_path.with_name(self.mutes_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"mutes": list(self.mutes.values())}, indent=1), encoding="utf-8")
            os.replace(tmp, self.mutes_path)
        except OSError:
            log.warning("could not save %s", self.mutes_path, exc_info=True)

    def _expire_mutes(self, now: float) -> None:
        changed = False
        for t, m in list(self.mutes.items()):
            if m["kind"] == "ack":
                a = self.active.get(t)
                if a is None:
                    m.setdefault("gone_at", now)
                    gone = now - m["gone_at"] > ACK_GONE_S
                else:
                    m.pop("gone_at", None)
                    gone = RANK.get(a["severity"], 9) < RANK.get(m.get("severity"), 9)  # worse than when acknowledged
                if gone:
                    del self.mutes[t]
                    changed = True
            elif m.get("until") is not None and m["until"] <= now:
                del self.mutes[t]
                changed = True
        if changed:
            self._save_mutes()

    def mute_of(self, a: dict, now: Optional[float] = None) -> Optional[dict]:
        now = time.time() if now is None else now
        for t in (a["key"], f"code:{a['code']}"):
            m = self.mutes.get(t)
            if m and (m["kind"] == "ack" or (m.get("until") or 0) > now):
                if m["kind"] == "ack" and RANK.get(a["severity"], 9) < RANK.get(m.get("severity"), 9):
                    continue
                return m
        return None

    def mute(self, key: str, hours: Optional[float] = None, every: bool = False, by: Optional[str] = None,
             now: Optional[float] = None) -> dict:
        """Acknowledge ``key`` (``hours`` None: until it clears or gets worse), or mute it, or with ``every`` all
        alerts with its code, for ``hours``. ``key`` must be an active alert."""
        now = time.time() if now is None else now
        a = self.active.get(key)
        if a is None:
            raise KeyError(key)
        if hours is None and every:
            raise ValueError("muting every alert of a kind needs a time")
        if hours is not None and not 0 < hours * 3600 <= MAX_MUTE_S:
            raise ValueError(f"mute for more than 0 and at most {MAX_MUTE_S // 86400} days")
        target = f"code:{a['code']}" if every else key
        m = {"target": target, "kind": "ack" if hours is None else "mute", "code": a["code"],
             "title": f"Every {a['code']} alert" if every else a["title"], "severity": a["severity"],
             "until": None if hours is None else now + hours * 3600, "by": by, "at": now}
        self.mutes[target] = m
        self._save_mutes()
        what = "acknowledged" if hours is None else f"muted for {hours:g} h" + (" (every alert with this code)" if every else "")
        log.info("alert %s %s by %s", target, what, by or "-")
        return m

    def unmute(self, target: str) -> bool:
        if self.mutes.pop(target, None) is None:
            return False
        self._save_mutes()
        log.info("alert %s unmuted", target)
        return True

    def _report(self, what: str, a: dict, now: float) -> None:
        self.recent.append({"at": now, "event": what, **{k: a[k] for k in ("key", "code", "severity", "area", "title")}})
        text = f"[{a['code']}] {a['title']}"
        if what == "raised":
            text += (f". {a['detail']}" if a.get("detail") else "") + (f"\nWhat to do: {a['hint']}" if a.get("hint") else "")
            log.log(logging.ERROR if a["severity"] == "error" else logging.WARNING, "alert: %s", text.replace("\n", " "))
            kind = winevent.ERROR if a["severity"] == "error" else winevent.WARNING
        else:
            text = "Cleared: " + text
            log.info("alert %s", text)
            kind = winevent.INFO
        try:
            self.eventlog(kind, winevent.event_id(a["area"], cleared=what == "cleared"), text)
        except Exception:  # the event log is a convenience; never let it break the list
            log.debug("event log write failed", exc_info=True)

    def list(self) -> list[dict]:
        return sorted(self.active.values(), key=lambda a: (RANK.get(a["severity"], 9), -(a["since"] or 0)))

    def view(self, now: Optional[float] = None) -> dict:
        """``alerts``: what needs attention (and counts); ``muted``: active alerts an admin muted; ``mutes``: every
        acknowledge and mute in force, including ones for a code with no alert right now."""
        now = time.time() if now is None else now
        shown, muted = [], []
        for a in self.list():
            m = self.mute_of(a, now)
            (muted if m else shown).append({**a, "muted": m} if m else a)
        return {"at": self.evaluated_at, "alerts": shown, "muted": muted,
                "mutes": sorted(self.mutes.values(), key=lambda m: -m["at"]),
                "counts": {**{s: sum(1 for a in shown if a["severity"] == s) for s in ("error", "warning")},
                           "muted": len(muted)},
                "recent": list(self.recent)[::-1][:50], "errors": dict(self.errors)}

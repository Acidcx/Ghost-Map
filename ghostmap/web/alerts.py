"""The alert list: every ``EVAL_S`` the rules in ``analysis/alerts.py`` run over what each part of Ghost Map knows.

Each alert keeps the time it started for as long as it lasts. A new alert, or one that gets worse, is written to
Ghost Map's log and the Windows Application event log (source "GhostMap"); so is an alert that clears. The web
UI shows the list as a banner (``GET /api/alerts``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from typing import Callable, Optional

from ghostmap import winevent

EVAL_S = 30.0
RECENT_KEPT = 200
FRESH_S = 10.0   # a page asking for the list gets one at most this old
log = logging.getLogger("ghostmap.alerts")
RANK = {"error": 0, "warning": 1}


class AlertCenter:
    def __init__(self, sources: dict[str, Callable], eventlog: Optional[Callable[[int, int, str], bool]] = None):
        """``sources``: name -> fn(now) returning alerts (or a coroutine of them)."""
        self.sources = sources
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
            return self.list()

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

    def view(self) -> dict:
        items = self.list()
        return {"at": self.evaluated_at, "alerts": items,
                "counts": {s: sum(1 for a in items if a["severity"] == s) for s in ("error", "warning")},
                "recent": list(self.recent)[::-1][:50], "errors": dict(self.errors)}

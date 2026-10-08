"""The resident collector: reads every saved dashboard about once a second, whether or not anyone is
looking, and feeds each read to the alarm history.

Reads go through ``LiveValues`` (the shared, read-only gateway sessions and their one-second cache), so a
person watching a dashboard and the collector cost the gateway one read, not two. Each dashboard has its
own loop, so a gateway that is down only slows the dashboards behind it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ghostmap.analysis.dashboard import node_ids
from ghostmap.history import History
from ghostmap.web.dashboards import DashboardStore, LiveValues, visible_node_ids

PERIOD_S = 1.0        # one read per dashboard per second
RESCAN_S = 5.0        # how often new, changed and deleted dashboards are picked up
ERROR_PAUSE_S = 5.0   # after an unexpected error in a loop
log = logging.getLogger("ghostmap.collector")


class Collector:
    def __init__(self, store: DashboardStore, live: LiveValues, history: History, period: float = PERIOD_S):
        self.store, self.live, self.history, self.period = store, live, history, period
        self._dash: dict[str, dict] = {}
        self._mtime: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._status: dict[str, dict] = {}
        self._super: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._super is None:
            self._super = asyncio.create_task(self._supervise(), name="ghostmap-collector")
            log.info("collector started: every dashboard read every %.1f s, history in %s", self.period, self.history.path)

    async def stop(self) -> None:
        tasks = [t for t in [self._super, *self._tasks.values()] if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._super, self._tasks = None, {}

    def forget(self, did: str) -> None:
        """A dashboard was deleted: stop reading it now rather than at the next rescan."""
        t = self._tasks.pop(did, None)
        if t:
            t.cancel()
        self._dash.pop(did, None)
        self._mtime.pop(did, None)
        self._status.pop(did, None)

    def status(self, did: str) -> dict:
        """Is the collector reading this dashboard, and when did it last succeed."""
        st = self._status.get(did) or {}
        return {"recording": did in self._tasks and not self._tasks[did].done(), **st}

    # ---------------------------------------------------------------------------------------------
    def _rescan(self) -> None:
        seen = set()
        for p in self.store.dir.glob("*.json") if self.store.dir.exists() else []:
            if p.name.endswith(".tags.json"):
                continue
            did = p.stem
            try:
                mtime = p.stat().st_mtime
                if self._mtime.get(did) != mtime:
                    self._dash[did] = self.store.load(did)  # edits (flips, hidden tags, layout) apply from here on
                    self._mtime[did] = mtime
            except (OSError, ValueError, KeyError) as exc:
                if did not in self._dash:
                    log.warning("collector: can't load dashboard %s: %s", did, exc)
                    continue
            seen.add(did)
        for did in list(self._tasks):
            if did not in seen:
                self.forget(did)
                self.history.forget(did)
        for did in seen:
            t = self._tasks.get(did)
            if t is None or t.done():
                self._tasks[did] = asyncio.create_task(self._loop(did), name=f"ghostmap-collect-{did}")

    async def _supervise(self) -> None:
        while True:
            try:
                self._rescan()
            except Exception:
                log.exception("collector rescan failed")
            await asyncio.sleep(RESCAN_S)

    async def _loop(self, did: str) -> None:
        while did in self._dash:
            t0 = time.monotonic()
            try:
                dash = self._dash[did]
                r = await self.live.read(dash, visible_node_ids(dash, node_ids(dash["layout"])))
                at = r.get("at") if r.get("ok") else None
                self.history.observe(dash, r, now=at)
                st = self._status.setdefault(did, {})
                st["last_read"] = time.time()
                if r.get("ok"):
                    st["last_ok"] = at
                st["error"] = r.get("error")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("collector: dashboard %s cycle failed", did)
                self._status.setdefault(did, {})["error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(ERROR_PAUSE_S)
            await asyncio.sleep(max(0.05, self.period - (time.monotonic() - t0)))

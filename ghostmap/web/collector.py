"""The resident collector: takes every saved dashboard's values about once a second, whether or not anyone
is looking, and feeds them to the alarm history.

Values come through ``LiveValues`` (the shared, read-only gateway sessions, each dashboard's subscription and
the one-second cache), so a person watching a dashboard and the collector cost the gateway nothing extra.

With a subscription the history runs a little behind the gateway (one sample and one publish interval plus
half a second, about 1.75 s by default): a change only reaches Ghost Map with the next publish, so the history
waits until every change up to a moment is sure to have arrived, then plays them in order at the time they
happened. An alarm that came and went within a second is still recorded with its real length, and two alarms
a few hundred milliseconds apart get the right first-out. Each dashboard has its own loop, so a gateway that
is down only slows the dashboards behind it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ghostmap.analysis.dashboard import node_ids
from ghostmap.history import History
from ghostmap.web.dashboards import DashboardStore, LiveValues, feed_lag_s, visible_node_ids

PERIOD_S = 1.0        # one read per dashboard per second
REPLAY_MAX = 200      # changes replayed one at a time per cycle; past this (a storm) they are applied in one step
CLOCK_JUMP_S = 60.0   # the clock went back further than this: the history follows it rather than wait
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
        self._t: dict[str, float] = {}        # per dashboard: time of the last state given to the history
        self._follow: dict[str, dict] = {}    # per subscribed dashboard: the history's state and changes not yet due

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
        self._t.pop(did, None)
        self._follow.pop(did, None)

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
                # Always a fresh read (a page's read from a moment ago is fine): with the one-second cache a
                # read exactly a period later could be served from cache, halving the history's resolution.
                # With a subscription a "read" is a copy of what the gateway already sent, so always take it fresh:
                # a cached copy would be older than the changes drained with it.
                fresh = 0.0 if self.live.subscribed(did) else self.period / 4
                r = await self.live.read(dash, visible_node_ids(dash, node_ids(dash["layout"])), max_age=fresh)
                at = r.get("at") if r.get("ok") else None
                # No await between the read and the drain: the snapshot already holds every change drained here.
                changes = self.live.take_changes(did)
                if r.get("ok") and self.live.subscribed(did):
                    self.follow(dash, r, changes, feed_lag_s(dash))
                else:
                    self.flush(dash, changes, at)  # a subscription that just ended: what it reported still counts
                    self.history.observe(dash, r, now=at)
                    self._t[did] = at or time.time()
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

    def follow(self, dash: dict, result: dict, changes: list[tuple], lag: float) -> None:
        """One cycle of a subscribed dashboard. ``changes`` are ``(time, node_id, value, status)`` as the gateway
        reported them; the history is given every change up to ``lag`` seconds ago, one instant at a time, then
        the state at that moment."""
        did, at = dash["id"], result["at"]
        bad = set(result.get("bad") or [])
        f = self._follow.get(did)
        if f is None:  # the first cycle: the snapshot is where the history starts
            f = self._follow[did] = {"values": dict(result.get("values") or {}), "bad": bad, "pending": []}
        else:
            f["pending"].extend(changes)
            values, b = dict(f["values"]), set(f["bad"])
            for _, nid, v, status in f["pending"]:
                values[nid] = v
                _mark(b, nid, status)
            # Whatever the snapshot shows that no notification explained (a new subscription after a reconnect,
            # tags added to the layout) is taken as a change now.
            for nid, v in (result.get("values") or {}).items():
                if nid not in values or not _same(values[nid], v) or (nid in b) != (nid in bad):
                    f["pending"].append((at, nid, v, "Bad" if nid in bad else "Good"))
        last = self._t.get(did, 0.0)
        if last - at > CLOCK_JUMP_S:  # the PC's clock was set back: carry on from the new time
            last = self._t[did] = 0.0
        cutoff = max(at - lag, last)
        f["pending"].sort(key=lambda c: c[0])
        due = [c for c in f["pending"] if c[0] <= cutoff]
        f["pending"] = f["pending"][len(due):]
        self._play(dash, f, due, cutoff)
        self.history.observe(dash, {"ok": True, "values": f["values"], "bad": list(f["bad"])}, now=cutoff)
        self._t[did] = cutoff

    def flush(self, dash: dict, changes: list[tuple] = (), until: Optional[float] = None) -> None:
        """The subscription ended (connection lost, switched to polling): play what it had reported."""
        f = self._follow.pop(dash["id"], None)
        if f:
            f["pending"].extend(changes)
            f["pending"].sort(key=lambda c: c[0])
            self._play(dash, f, f["pending"], until or time.time())

    def _play(self, dash: dict, f: dict, changes: list[tuple], end: float) -> None:
        """Give the history each instant in ``changes`` (sorted by time) in turn; times before the last state it
        was given are moved up to it."""
        did = dash["id"]
        last = self._t.get(did, 0.0)
        if len(changes) > REPLAY_MAX:  # a storm: apply the oldest ones in one step
            for _, nid, v, status in changes[:-REPLAY_MAX]:
                f["values"][nid] = v
                _mark(f["bad"], nid, status)
            changes = changes[-REPLAY_MAX:]
        i = 0
        while i < len(changes):
            t = min(max(changes[i][0], last), end)
            while i < len(changes) and min(max(changes[i][0], last), end) == t:  # one instant together
                _, nid, v, status = changes[i]
                f["values"][nid] = v
                _mark(f["bad"], nid, status)
                i += 1
            self.history.observe(dash, {"ok": True, "values": f["values"], "bad": list(f["bad"])}, now=t)
            last = self._t[did] = t


def _mark(bad: set, nid: str, status) -> None:
    (bad.discard if str(status).startswith("Good") else bad.add)(nid)


def _same(a, b) -> bool:
    return a == b or (a != a and b != b)  # NaN equals NaN here

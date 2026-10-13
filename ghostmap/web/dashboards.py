"""Saved machine dashboards and the read-only OPC UA reads that feed them.

A dashboard is a layout built by ``analysis/dashboard.py`` from a tag export,
plus the endpoint to read from and the user's overrides (areas flipped to
"on = healthy", hidden tags). Saved as ``<data_dir>/dashboards/<id>.json``.

Live values: the server keeps one anonymous OPC UA session per gateway, shared
by every dashboard on that gateway. By default each dashboard holds one OPC UA
subscription on its tags: the gateway samples them and sends only what changed,
so a second of nothing happening costs nothing, and short changes are queued
rather than missed between reads. Every ``CHECK_S`` a one-value read proves the
session is alive, and every ``FULL_READ_S`` a full read checks the subscription
missed nothing. If the gateway refuses a subscription, or a dashboard is set to
"poll", its tags are read in full once a second instead, as before.

Either way each dashboard's answer is cached for ``CACHE_S``, so several people
watching the same machine cost the gateway nothing extra. Sessions nothing has
read for ``IDLE_S`` are closed (the background collector reads every dashboard,
so in normal running they stay open).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ghostmap.analysis.dashboard import DEFAULT_MAX_READ_MS, DEFAULT_SAMPLE_MS, HEARTBEAT_STALE_S

CACHE_S = 1.0
IDLE_S = 300
READS_IN_FLIGHT = 4  # read requests sent to the gateway at once (a whole controller is ~10 requests of 200 tags)
EVENTS_KEPT = 200   # connection history kept per dashboard
BAD_KEPT = 50       # unreadable tags listed per dashboard (with their status)
FAIL_HOLD_S = 5.0   # after a failed connect or read, answer waiting requests with that failure for this long
CHECK_S = 5.0       # subscription mode: a one-value read this often proves the session is alive
FULL_READ_S = 60.0  # subscription mode: a full read this often checks that no change was missed
PUBLISH_STALE_S = 30.0  # no publish answer (data or keep-alive) for this long: the subscription is rebuilt
SUB_RETRY_S = 600.0     # after the gateway refuses a subscription, poll for this long before asking again
log = logging.getLogger("ghostmap.live")
_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def error_text(exc: BaseException) -> str:
    """A readable one-line reason. A bare timeout has no message of its own."""
    msg = str(exc).strip()
    if not msg and isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        msg = "no answer from the gateway in time"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _replace(src: Path, dst: Path, tries: int = 40) -> None:
    """os.replace that rides out Windows sharing violations.

    On Windows the replace fails with "Access is denied" while anything has ``dst`` open: the dashboard being
    read by a live-values request at that moment, or a virus scanner. The open is short, so retry briefly.
    """
    for i in range(tries):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


class DashboardStore:
    def __init__(self, root: Path):
        self.dir = Path(root) / "dashboards"

    def _path(self, dash_id: str) -> Path:
        if not _ID_RE.match(dash_id):
            raise KeyError(dash_id)
        return self.dir / f"{dash_id}.json"

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            if p.name.endswith(".tags.json"):
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out.append({"id": d["id"], "name": d["name"], "endpoint": d["endpoint"], "created": d["created"],
                            "summary": d["layout"]["summary"]})
            except (OSError, ValueError, KeyError, TypeError):
                continue  # one damaged file must not hide every other dashboard
        return out

    def load(self, dash_id: str) -> dict:
        p = self._path(dash_id)
        if not p.exists():
            raise KeyError(dash_id)
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, dash: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(dash["id"])
        tmp = p.with_name(f"{p.stem}.{secrets.token_hex(4)}.tmp")  # two saves at once must not share a tmp file
        tmp.write_text(json.dumps(dash, indent=1), encoding="utf-8")
        _replace(tmp, p)

    def save_tags(self, dash_id: str, rows: list[dict]) -> None:
        """The full tag export behind a dashboard, so edits pick tags from what was discovered."""
        p = self._path(dash_id).with_suffix(".tags.json")
        slim = [[r.get("path", ""), r["node_id"], r.get("type") or r.get("variant_type") or "", r.get("value")]
                for r in rows if r.get("node_id")]
        p.write_text(json.dumps(slim), encoding="utf-8")

    def load_tags(self, dash_id: str) -> list[list]:
        p = self._path(dash_id).with_suffix(".tags.json")
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))

    def create(self, name: str, endpoint: str, source: str, layout: dict) -> dict:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "machine"
        dash = {"id": f"{slug}-{secrets.token_hex(3)}", "name": name, "endpoint": endpoint, "source": source,
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "layout": layout, "overrides": {"invert": {}, "hidden": []}}
        self.save(dash)
        return dash

    def delete(self, dash_id: str) -> None:
        p = self._path(dash_id)
        if not p.exists():
            raise KeyError(dash_id)
        p.unlink()
        p.with_suffix(".tags.json").unlink(missing_ok=True)


def endpoint_key(endpoint: str) -> str:
    """Dashboards whose endpoints match here share one gateway session."""
    return str(endpoint or "").strip().lower()


class LiveValues:
    """Read-only OPC UA sessions, one per gateway, plus a per-dashboard record of how well it is reading.

    Comms health (per dashboard): how many tags came back Good on the last read, read latency, when the
    session dropped and reconnected and why, and when the heartbeat tag last changed. A dropped session is
    re-opened straight away and the read retried once, so a single drop costs one slow read, not a gap.
    """

    def __init__(self, resolve_url: Callable[[str], Awaitable[str]]):
        self.resolve_url = resolve_url
        self.trust = None  # ua_trust.CertTrust: each gateway's certificate is checked against its pin
        self._s: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # One session per gateway: {"browser", "lock" (held while connecting), "gen" (bumped on each new session)}.
        self._conns: dict[str, dict] = {}

    def _state(self, did: str, dash: dict | None = None) -> dict:
        s = self._s.get(did)
        if s is None:
            now = time.time()
            s = self._s[did] = {
                "key": "", "gen": None, "since": {}, "last": {}, "at": 0, "result": None, "used": now, "started": None,
                "events": deque(maxlen=EVENTS_KEPT),
                "health": {"connected_at": None, "cycles": 0, "failed": 0, "drops": 0, "reconnects": 0,
                           "last_ok": None, "last_error": None, "latency_ms": None, "latency_max_ms": 0,
                           "latency_avg_ms": None, "good": 0, "total": 0, "bad_by_plc": {}, "bad_status": {},
                           "last_any_change": None}}
        if dash is not None:
            s["key"] = endpoint_key(dash.get("endpoint"))
        return s

    def _conn(self, s: dict) -> dict:
        c = self._conns.get(s["key"])
        if c is None:
            c = self._conns[s["key"]] = {"browser": None, "lock": asyncio.Lock(), "gen": 0, "url": None}
        return c

    def _event(self, s: dict, did: str, kind: str, detail: str = "") -> None:
        ev = s["events"][-1] if s["events"] else None
        if ev and ev["kind"] == kind and ev["detail"] == detail:  # the same failure every poll: count it, don't log it
            ev["repeats"] = ev.get("repeats", 1) + 1
            ev["last"] = time.time()
            return
        s["events"].append({"at": time.time(), "kind": kind, "detail": detail})
        level = logging.WARNING if kind in ("drop", "error") else logging.INFO
        log.log(level, "dashboard %s: %s %s", did, kind, detail)

    async def read(self, dash: dict, node_ids: list[str], max_age: float = CACHE_S) -> dict:
        """The dashboard's values, from a read no older than ``max_age`` seconds (shared by everyone asking)."""
        did = dash["id"]
        lock = self._locks.setdefault(did, asyncio.Lock())
        async with lock:
            await self._drop_idle(keep=did)
            s = self._state(did, dash)
            now = time.time()
            s["used"] = now
            if s.get("result") and now - s["at"] < max_age:
                return s["result"]
            # The gateway just failed: don't queue another 15 s connect attempt behind every waiting request.
            if s.get("fail") and now - s["fail_at"] < FAIL_HOLD_S:
                return {**s["fail"], "health": self._health(dash, s)}
            try:
                values, bad, latency = await self._fetch(dash, s, node_ids)
            except Exception as exc:
                h = s["health"]
                h["failed"] += 1
                h["last_error"] = error_text(exc)
                s["result"] = None
                s["fail"] = {"ok": False, "error": h["last_error"], "values": {}, "since": {}, "bad": []}
                s["fail_at"] = time.time()
                return {**s["fail"], "health": self._health(dash, s)}
            s["fail"] = None
            # When did each value last change (as seen by Ghost Map)? Shown as "since" on active alarms.
            if s["started"] is None:
                s["started"] = now
            h = s["health"]
            for nid, v in values.items():
                if nid in s["last"] and s["last"][nid] != v:
                    s["since"][nid] = now
                    h["last_any_change"] = now
                s["last"][nid] = v
            self._account(s, did, values, bad, latency, now)
            result = {"ok": True, "error": None, "values": values, "bad": list(bad),
                      "since": {k: v for k, v in s["since"].items() if k in values},
                      "watching_since": s["started"], "at": now}
            result["health"] = self._health(dash, s)
            s.update(at=now, result=result)
            return result

    async def _fetch(self, dash: dict, s: dict, node_ids: list[str]) -> tuple[dict, dict, Optional[float]]:
        """The dashboard's values: from its subscription when it has one, else a full read."""
        from ghostmap.collectors.opcua import SubscriptionRefused

        if read_mode(dash) == "subscribe" and time.time() >= s.get("sub_retry_at", 0):
            try:
                return await self._with_session(dash, s, lambda b: self._from_feed(dash, s, b, node_ids))
            except SubscriptionRefused as exc:
                s["sub_retry_at"] = time.time() + SUB_RETRY_S
                s["sub_error"] = str(exc)
                self._event(s, dash["id"], "subscribe", f"subscription refused, reading every second instead: {exc}")
        elif s.get("feed") is not None:  # switched to polling: let the gateway drop the subscription
            await self._drop_feed(s)
        return await self._with_session(dash, s, lambda b: self._timed_read(b, node_ids))

    async def _timed_read(self, browser, node_ids: list[str]) -> tuple[dict, dict, float]:
        t0 = time.perf_counter()
        values, bad = await self._read_all(browser, node_ids)
        return values, bad, (time.perf_counter() - t0) * 1000

    async def _from_feed(self, dash: dict, s: dict, browser, node_ids: list[str]) -> tuple[dict, dict, Optional[float]]:
        from ghostmap.collectors.opcua import SERVER_STATE

        if not browser.connected():  # the socket closed under us: treat it as a drop now, not at the next check
            raise ConnectionError("the gateway closed the connection")
        feed = s.get("feed")
        stale = feed is not None and (feed.publish_age() or 0) > PUBLISH_STALE_S
        if feed is not None and (s.get("feed_gen") != s["gen"] or feed.dead or stale or feed.ids != list(dict.fromkeys(node_ids))):
            why = feed.dead or ("no answer from the gateway's publish for a while" if stale else "")
            if why:
                self._event(s, dash["id"], "subscribe", f"re-subscribing: {why}")
            await self._drop_feed(s)
            feed = None
        now = time.time()
        if feed is None:
            sample, publish = feed_timing(dash)
            t0 = time.perf_counter()
            feed = await browser.subscribe(node_ids, sample_ms=sample, publish_ms=publish)
            feed.seed(await self._read_rows(browser, node_ids))
            latency = (time.perf_counter() - t0) * 1000
            feed.last_check = feed.last_full = now
            s.update(feed=feed, feed_gen=s["gen"], sub_error=None)
            s["health"]["subscribed_at"] = now
            self._event(s, dash["id"], "subscribe", f"subscribed to {len(feed.ids)} tags, sampled every {sample} ms"
                        + (f"; the gateway would not monitor {len(feed.refused)}" if feed.refused else ""))
        elif now - feed.last_full >= FULL_READ_S:
            t0 = time.perf_counter()
            rows = await self._read_rows(browser, node_ids)
            latency = (time.perf_counter() - t0) * 1000
            n = feed.reconcile(rows, now)
            s["health"]["reconciled"] = s["health"].get("reconciled", 0) + n
            feed.last_full = feed.last_check = now
        elif now - feed.last_check >= CHECK_S:
            t0 = time.perf_counter()
            await asyncio.wait_for(browser.read([SERVER_STATE]), timeout=15)
            latency = (time.perf_counter() - t0) * 1000
            feed.last_check = now
        else:
            latency = None  # nothing was asked of the gateway: the subscription delivered the values
        return dict(feed.values), dict(feed.bad), latency

    async def _drop_feed(self, s: dict) -> None:
        feed = s.pop("feed", None)
        if feed is not None:
            s["dropped_changes"] = s.get("dropped_changes", 0) + feed.dropped
            await feed.close()

    def subscribed(self, did: str) -> bool:
        s = self._s.get(did)
        return bool(s and s.get("feed") is not None)

    def take_changes(self, did: str) -> list[tuple]:
        """Every value change the subscription saw since the last call, oldest first (for the alarm history)."""
        s = self._s.get(did)
        feed = s.get("feed") if s else None
        return feed.drain() if feed is not None else []

    async def _with_session(self, dash: dict, s: dict, op):
        """Run ``op(browser)`` on the gateway's session, re-opening it once if it dropped."""
        from ghostmap.collectors.opcua import SubscriptionRefused, is_connection_error

        did = dash["id"]
        for attempt in (1, 2):
            browser = None
            try:
                browser = await self._ensure(dash, s)
                return await op(browser)
            except SubscriptionRefused:
                raise
            except Exception as exc:
                reason = error_text(exc)
                was_up = browser is not None
                if was_up and not is_connection_error(exc):
                    # The gateway refused the request, but the session is fine: keep it, don't count a drop.
                    self._event(s, did, "error", f"read refused: {reason}")
                    raise
                s.pop("feed", None)  # a subscription dies with its session
                await self._close_browser(s, browser)
                if was_up:
                    s["health"]["drops"] += 1
                    self._event(s, did, "drop", reason)
                else:
                    self._event(s, did, "error", f"connect failed: {reason}")
                if attempt == 2 or not was_up:
                    raise
        raise RuntimeError("unreachable")

    def _account(self, s: dict, did: str, values: dict, bad: dict, latency: float, now: float) -> None:
        from ghostmap.analysis.dashboard import plc_of

        h = s["health"]
        h["cycles"] += 1
        h["last_ok"] = now
        h["last_error"] = None
        if latency is not None:  # None: served from the subscription, nothing was timed
            h["latency_ms"] = round(latency, 1)
            h["latency_max_ms"] = round(max(h["latency_max_ms"], latency), 1)
            h["latency_avg_ms"] = round(latency if h["latency_avg_ms"] is None else 0.9 * h["latency_avg_ms"] + 0.1 * latency, 1)
        h["total"], h["good"] = len(values), len(values) - len(bad)
        by_plc: dict[str, int] = {}
        for nid in bad:
            by_plc[plc_of(nid)] = by_plc.get(plc_of(nid), 0) + 1
        if by_plc != h["bad_by_plc"]:
            if by_plc:
                self._event(s, did, "coverage", f"{len(bad)} of {len(values)} tags not Good: "
                            + ", ".join(f"{k} {v}" for k, v in sorted(by_plc.items())))
            elif h["bad_by_plc"]:
                self._event(s, did, "coverage", f"all {len(values)} tags Good again")
        h["bad_by_plc"] = by_plc
        h["bad_status"] = dict(list(bad.items())[:BAD_KEPT])
        s["bad_ids"] = set(bad)

    def _health(self, dash: dict, s: dict) -> dict:
        now = time.time()
        h = dict(s["health"])
        m = (dash.get("layout") or {}).get("machine") or {}
        hb = m.get("heartbeat") or []
        h["freshness"] = m.get("freshness") or ("heartbeat" if hb else "response")
        h["max_read_ms"] = m.get("max_read_ms") or DEFAULT_MAX_READ_MS
        # Response-time mode: no heartbeat; data counts as fresh while reads come back Good and quickly.
        h["slow"] = h["latency_ms"] is not None and h["latency_ms"] > h["max_read_ms"]
        if h["freshness"] == "response":
            h["heartbeat"] = {"node_id": None, "frozen": False}
        elif hb and s["started"] is not None:
            nid = hb[0]
            changed = s["since"].get(nid)
            age = now - (changed or s["started"])
            h["heartbeat"] = {"node_id": nid, "value": s["last"].get(nid), "changed_at": changed,
                              "age_s": round(age, 1), "good": nid in s["last"] and nid not in s.get("bad_ids", ()),
                              "frozen": age > HEARTBEAT_STALE_S}
        else:
            h["heartbeat"] = {"node_id": hb[0] if hb else None, "frozen": False}
        h["started"] = s["started"]
        feed = s.get("feed")
        h["mode"] = "subscription" if feed is not None else "polling"
        h["read_mode"] = read_mode(dash)
        h["sub_error"] = s.get("sub_error")
        if feed is not None:
            h["notifications"] = feed.notifications
            h["not_monitored"] = len(feed.refused)
            h["changes_dropped"] = s.get("dropped_changes", 0) + feed.dropped
            h["sample_ms"] = int(m.get("sample_ms") or DEFAULT_SAMPLE_MS)
            h["history_lag_s"] = round(feed_lag_s(dash), 2)
        c = self._conns.get(s["key"])
        h["connected"] = bool(c and c["browser"] is not None and s["gen"] == c["gen"])
        changed = self.trust.warning(c["url"]) if self.trust is not None and c else None
        h["cert_changed"] = {k: changed.get(k) for k in ("sha256", "first_seen", "subject")} if changed else None
        h["shared_with"] = sum(1 for o in self._s.values() if o is not s and o["key"] == s["key"])
        return h

    def health(self, dash: dict) -> dict:
        """Comms health and the connection history, without reading anything."""
        s = self._state(dash["id"], dash)
        return {**self._health(dash, s), "events": list(s["events"])[::-1]}

    def history(self, dash: dict) -> dict:
        """What the live reads have seen, for verify_layout()."""
        s = self._s.get(dash["id"]) or {}
        return {"since": dict(s.get("since", {})), "started": s.get("started"), "now": time.time()}

    async def _ensure(self, dash: dict, s: dict):
        """The gateway's shared session, opened if needed. Returns the browser to read with."""
        from ghostmap.collectors.opcua import UaBrowser

        c = self._conn(s)
        if c["browser"] is None:
            async with c["lock"]:  # several dashboards may find it closed at once; only one connects
                if c["browser"] is None:
                    browser = UaBrowser(await self.resolve_url(dash["endpoint"]))
                    try:
                        await asyncio.wait_for(browser.connect(trust=self.trust), timeout=15)
                    except BaseException:
                        await browser.disconnect()
                        raise
                    c.update(browser=browser, gen=c["gen"] + 1, url=browser.url)
                    log.info("gateway session %d open to %s", c["gen"], browser.url)
                    if browser.cert_warning:
                        log.warning("gateway %s: %s", browser.url, browser.cert_warning)
        # This dashboard's view of it: first connection, or a new session after a drop.
        if s["gen"] != c["gen"]:
            if s["gen"] is None:
                self._event(s, dash["id"], "connected", c["url"] or "")
            else:
                s["health"]["reconnects"] += 1
                self._event(s, dash["id"], "reconnected")
            if c["browser"].cert_warning:
                self._event(s, dash["id"], "certificate", c["browser"].cert_warning)
            s["gen"] = c["gen"]
            s["health"]["connected_at"] = time.time()
        return c["browser"]

    async def _close_browser(self, s: dict, browser) -> None:
        """Close the gateway session after a drop, unless another dashboard already replaced it."""
        c = self._conns.get(s["key"])
        if browser is None or c is None or c["browser"] is not browser:
            return
        c["browser"] = None
        await browser.disconnect()

    @staticmethod
    async def _read_all(browser, node_ids: list[str]) -> tuple[dict, dict]:
        rows = await LiveValues._read_rows(browser, node_ids)
        values = {r["node_id"]: r["value"] for r in rows}
        bad = {r["node_id"]: r["status"] for r in rows if not str(r["status"]).startswith("Good")}
        return values, bad

    @staticmethod
    async def _read_rows(browser, node_ids: list[str]) -> list[dict]:
        from ghostmap.collectors.opcua import MAX_READ

        if browser is None:
            raise ConnectionError("session closed")
        sem = asyncio.Semaphore(READS_IN_FLIGHT)

        async def one(chunk):
            async with sem:
                return await asyncio.wait_for(browser.read(chunk), timeout=15)

        chunks = [node_ids[i:i + MAX_READ] for i in range(0, len(node_ids), MAX_READ)]
        return [r for part in await asyncio.gather(*(one(c) for c in chunks)) for r in part]

    async def read_once(self, dash: dict, node_ids: list[str], rows: bool = False) -> dict:
        """A one-off read on the dashboard's session (an axis's fault bits, or the alarm check).

        ``rows=True`` also returns each tag's status and data type.
        """
        from ghostmap.collectors.opcua import is_connection_error

        lock = self._locks.setdefault(dash["id"], asyncio.Lock())
        async with lock:
            s = self._state(dash["id"], dash)
            s["used"] = time.time()
            for attempt in (1, 2):
                browser = None
                try:
                    browser = await self._ensure(dash, s)
                    got = await self._read_rows(browser, node_ids)
                    break
                except Exception as exc:
                    was_up = browser is not None
                    if not (was_up and not is_connection_error(exc)):
                        await self._close_browser(s, browser)
                        if was_up:
                            s["health"]["drops"] += 1
                            self._event(s, dash["id"], "drop", error_text(exc))
                    if attempt == 2 or not (was_up and is_connection_error(exc)):
                        return {"ok": False, "error": error_text(exc), "values": {}, "bad": []}
            out = {"ok": True, "error": None, "values": {r["node_id"]: r["value"] for r in got},
                   "bad": [r["node_id"] for r in got if not str(r["status"]).startswith("Good")]}
            if rows:
                out["rows"] = {r["node_id"]: {"status": r["status"], "variant_type": r["variant_type"], "value": r["value"]}
                               for r in got}
            return out

    async def _drop_idle(self, keep: str) -> None:
        now = time.time()
        for did, s in list(self._s.items()):
            lock = self._locks.get(did)
            if did != keep and now - s.get("used", 0) > IDLE_S and not (lock and lock.locked()):
                self._s.pop(did, None)
                await self._drop_feed(s)
        await self._close_unused()

    async def _close_unused(self) -> None:
        """Close gateway sessions no remaining dashboard reads through."""
        in_use = {s["key"] for s in self._s.values()}
        for key, c in list(self._conns.items()):
            if key not in in_use and not c["lock"].locked():
                self._conns.pop(key, None)
                if c["browser"] is not None:
                    await c["browser"].disconnect()

    async def forget(self, dash_id: str) -> None:
        s = self._s.pop(dash_id, None)
        if s is not None:
            await self._drop_feed(s)
        self._locks.pop(dash_id, None)
        await self._close_unused()

    async def close(self) -> None:
        self._s.clear()
        await self._close_unused()

    def sessions(self) -> list[dict]:
        """Open gateway sessions and how many dashboards share each, for the debug bundle."""
        return [{"endpoint": key, "url": c["url"], "open": c["browser"] is not None, "session": c["gen"],
                 "dashboards": sum(1 for s in self._s.values() if s["key"] == key)}
                for key, c in self._conns.items()]

    def snapshot(self) -> dict:
        """Health of every live dashboard session, for the debug bundle (no tag values)."""
        out = {}
        for did, s in self._s.items():
            h = dict(s["health"])
            h["events"] = list(s["events"])
            out[did] = h
        return out


def read_mode(dash: dict) -> str:
    """"subscribe" (the default) or "poll", from the dashboard's machine settings."""
    m = (dash.get("layout") or {}).get("machine") or {}
    return "poll" if m.get("read_mode") == "poll" else "subscribe"


def feed_timing(dash: dict) -> tuple[int, int]:
    """(sampling, publishing) interval in ms for the dashboard's subscription."""
    m = (dash.get("layout") or {}).get("machine") or {}
    sample = int(m.get("sample_ms") or DEFAULT_SAMPLE_MS)
    return sample, max(1000, sample)


def feed_lag_s(dash: dict) -> float:
    """How long after a change its notification is sure to have arrived: one sample, one publish and a margin."""
    sample, publish = feed_timing(dash)
    return (sample + publish) / 1000 + 0.5


def visible_node_ids(dash: dict, all_ids: list[str]) -> list[str]:
    hidden = set(dash.get("overrides", {}).get("hidden", []))
    return [n for n in all_ids if n not in hidden]


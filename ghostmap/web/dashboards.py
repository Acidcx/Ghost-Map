"""Saved machine dashboards and the read-only OPC UA reads that feed them.

A dashboard is a layout built by ``analysis/dashboard.py`` from a tag export,
plus the endpoint to read from and the user's overrides (areas flipped to
"on = healthy", hidden tags). Saved as ``<data_dir>/dashboards/<id>.json``.

Live values: the server keeps one anonymous OPC UA session per dashboard and
caches each read for ``CACHE_S``, so several people watching the same machine
cost the gateway one read per second, not one each. Sessions nobody has looked
at for ``IDLE_S`` are closed.
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
from typing import Awaitable, Callable

from ghostmap.analysis.dashboard import DEFAULT_MAX_READ_MS, HEARTBEAT_STALE_S

CACHE_S = 1.0
IDLE_S = 300
READS_IN_FLIGHT = 4  # read requests sent to the gateway at once (a whole controller is ~10 requests of 200 tags)
EVENTS_KEPT = 200   # connection history kept per dashboard
BAD_KEPT = 50       # unreadable tags listed per dashboard (with their status)
log = logging.getLogger("ghostmap.live")
_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")


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
            except (OSError, ValueError):
                continue
            out.append({"id": d["id"], "name": d["name"], "endpoint": d["endpoint"], "created": d["created"],
                        "summary": d["layout"]["summary"]})
        return out

    def load(self, dash_id: str) -> dict:
        p = self._path(dash_id)
        if not p.exists():
            raise KeyError(dash_id)
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, dash: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(dash["id"])
        tmp = p.with_suffix(".tmp")
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


class LiveValues:
    """One cached, read-only OPC UA session per dashboard, plus a record of how well it is reading.

    Comms health (per dashboard): how many tags came back Good on the last read, read latency, when the
    session dropped and reconnected and why, and when the heartbeat tag last changed. A dropped session is
    re-opened straight away and the read retried once, so a single drop costs one slow read, not a gap.
    """

    def __init__(self, resolve_url: Callable[[str], Awaitable[str]]):
        self.resolve_url = resolve_url
        self._s: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _state(self, did: str) -> dict:
        s = self._s.get(did)
        if s is None:
            now = time.time()
            s = self._s[did] = {
                "browser": None, "since": {}, "last": {}, "at": 0, "result": None, "used": now, "started": None,
                "events": deque(maxlen=EVENTS_KEPT),
                "health": {"connected_at": None, "cycles": 0, "failed": 0, "drops": 0, "reconnects": 0,
                           "last_ok": None, "last_error": None, "latency_ms": None, "latency_max_ms": 0,
                           "latency_avg_ms": None, "good": 0, "total": 0, "bad_by_plc": {}, "bad_status": {},
                           "last_any_change": None}}
        return s

    def _event(self, s: dict, did: str, kind: str, detail: str = "") -> None:
        ev = s["events"][-1] if s["events"] else None
        if ev and ev["kind"] == kind and ev["detail"] == detail:  # the same failure every poll: count it, don't log it
            ev["repeats"] = ev.get("repeats", 1) + 1
            ev["last"] = time.time()
            return
        s["events"].append({"at": time.time(), "kind": kind, "detail": detail})
        level = logging.WARNING if kind in ("drop", "error") else logging.INFO
        log.log(level, "dashboard %s: %s %s", did, kind, detail)

    async def read(self, dash: dict, node_ids: list[str]) -> dict:
        did = dash["id"]
        lock = self._locks.setdefault(did, asyncio.Lock())
        async with lock:
            await self._drop_idle(keep=did)
            s = self._state(did)
            now = time.time()
            s["used"] = now
            if s.get("result") and now - s["at"] < CACHE_S:
                return s["result"]
            try:
                values, bad, latency = await self._read_with_retry(dash, s, node_ids)
            except Exception as exc:
                h = s["health"]
                h["failed"] += 1
                h["last_error"] = f"{type(exc).__name__}: {exc}"
                s["result"] = None
                return {"ok": False, "error": h["last_error"], "values": {}, "since": {}, "bad": [],
                        "health": self._health(dash, s)}
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

    async def _read_with_retry(self, dash: dict, s: dict, node_ids: list[str]) -> tuple[dict, dict, float]:
        from ghostmap.collectors.opcua import is_connection_error

        did = dash["id"]
        for attempt in (1, 2):
            reconnecting = s["browser"] is None and s["health"]["connected_at"] is not None
            try:
                await self._ensure(dash, s)
                if reconnecting:
                    s["health"]["reconnects"] += 1
                    self._event(s, did, "reconnected")
                t0 = time.perf_counter()
                values, bad = await self._read_all(s, node_ids)
                return values, bad, (time.perf_counter() - t0) * 1000
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                was_up = s["browser"] is not None
                await self._close(s)
                if was_up:
                    s["health"]["drops"] += 1
                    self._event(s, did, "drop", reason)
                else:
                    self._event(s, did, "error", f"connect failed: {reason}")
                if attempt == 2 or not (was_up and is_connection_error(exc)):
                    raise
        raise RuntimeError("unreachable")

    def _account(self, s: dict, did: str, values: dict, bad: dict, latency: float, now: float) -> None:
        from ghostmap.analysis.dashboard import plc_of

        h = s["health"]
        h["cycles"] += 1
        h["last_ok"] = now
        h["last_error"] = None
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
                              "age_s": round(age, 1), "good": nid in s["last"] and nid not in h["bad_status"],
                              "frozen": age > HEARTBEAT_STALE_S}
        else:
            h["heartbeat"] = {"node_id": hb[0] if hb else None, "frozen": False}
        h["started"] = s["started"]
        h["connected"] = s["browser"] is not None
        return h

    def health(self, dash: dict) -> dict:
        """Comms health and the connection history, without reading anything."""
        s = self._state(dash["id"])
        return {**self._health(dash, s), "events": list(s["events"])[::-1]}

    def history(self, dash: dict) -> dict:
        """What the live reads have seen, for verify_layout()."""
        s = self._s.get(dash["id"]) or {}
        return {"since": dict(s.get("since", {})), "started": s.get("started"), "now": time.time()}

    async def _ensure(self, dash: dict, s: dict) -> dict:
        from ghostmap.collectors.opcua import UaBrowser

        if s["browser"] is None:
            browser = UaBrowser(await self.resolve_url(dash["endpoint"]))
            try:
                await asyncio.wait_for(browser.connect(), timeout=15)
            except BaseException:
                await browser.disconnect()
                raise
            s["browser"] = browser
            first = s["health"]["connected_at"] is None
            s["health"]["connected_at"] = time.time()
            if first:
                self._event(s, dash["id"], "connected", browser.url)
        return s

    @staticmethod
    async def _close(s: dict) -> None:
        b, s["browser"] = s.get("browser"), None
        if b is not None:
            await b.disconnect()

    @staticmethod
    async def _read_all(s: dict, node_ids: list[str]) -> tuple[dict, dict]:
        rows = await LiveValues._read_rows(s, node_ids)
        values = {r["node_id"]: r["value"] for r in rows}
        bad = {r["node_id"]: r["status"] for r in rows if not str(r["status"]).startswith("Good")}
        return values, bad

    @staticmethod
    async def _read_rows(s: dict, node_ids: list[str]) -> list[dict]:
        from ghostmap.collectors.opcua import MAX_READ

        browser = s["browser"]
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
            s = self._state(dash["id"])
            s["used"] = time.time()
            for attempt in (1, 2):
                try:
                    await self._ensure(dash, s)
                    got = await self._read_rows(s, node_ids)
                    break
                except Exception as exc:
                    was_up = s["browser"] is not None
                    await self._close(s)
                    if was_up:
                        s["health"]["drops"] += 1
                        self._event(s, dash["id"], "drop", f"{type(exc).__name__}: {exc}")
                    if attempt == 2 or not (was_up and is_connection_error(exc)):
                        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "values": {}, "bad": []}
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
                await self._close(s)

    async def forget(self, dash_id: str) -> None:
        s = self._s.pop(dash_id, None)
        if s:
            await self._close(s)

    async def close(self) -> None:
        for did in list(self._s):
            await self.forget(did)

    def snapshot(self) -> dict:
        """Health of every live dashboard session, for the debug bundle (no tag values)."""
        out = {}
        for did, s in self._s.items():
            h = dict(s["health"])
            h["events"] = list(s["events"])
            out[did] = h
        return out


def visible_node_ids(dash: dict, all_ids: list[str]) -> list[str]:
    hidden = set(dash.get("overrides", {}).get("hidden", []))
    return [n for n in all_ids if n not in hidden]


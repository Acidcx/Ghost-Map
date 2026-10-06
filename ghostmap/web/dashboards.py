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
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

CACHE_S = 1.0
IDLE_S = 300
_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")


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
        tmp.replace(p)

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
    """One cached, read-only OPC UA session per dashboard."""

    def __init__(self, resolve_url: Callable[[str], Awaitable[str]]):
        self.resolve_url = resolve_url
        self._s: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def read(self, dash: dict, node_ids: list[str]) -> dict:
        did = dash["id"]
        lock = self._locks.setdefault(did, asyncio.Lock())
        async with lock:
            await self._drop_idle(keep=did)
            s = self._s.get(did)
            now = time.time()
            if s and s.get("result") and now - s["at"] < CACHE_S:
                s["used"] = now
                return s["result"]
            try:
                s = await self._ensure(dash, s)
                values, bad = await self._read_all(s, node_ids)
            except Exception as exc:
                if s and s.get("browser"):
                    await s["browser"].disconnect()
                    s["browser"] = None
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "values": {}, "since": {}, "bad": []}
            # When did each value last change (as seen by Ghost Map)? Shown as "since" on active alarms.
            for nid, v in values.items():
                if nid in s["last"] and s["last"][nid] != v:
                    s["since"][nid] = now
                s["last"][nid] = v
            result = {"ok": True, "error": None, "values": values, "bad": bad,
                      "since": {k: v for k, v in s["since"].items() if k in values},
                      "watching_since": s.setdefault("started", now), "at": now}
            s.update(at=now, used=now, result=result)
            return result

    async def _ensure(self, dash: dict, s: Optional[dict]) -> dict:
        from ghostmap.collectors.opcua import UaBrowser

        if s is None or s.get("browser") is None:
            browser = UaBrowser(await self.resolve_url(dash["endpoint"]))
            await asyncio.wait_for(browser.connect(), timeout=15)
            s = self._s[dash["id"]] = {"browser": browser, "since": {}, "last": {}, "at": 0, "result": None,
                                       "used": time.time()}
        return s

    @staticmethod
    async def _read_all(s: dict, node_ids: list[str]) -> tuple[dict, list]:
        from ghostmap.collectors.opcua import MAX_READ

        values, bad = {}, []
        for i in range(0, len(node_ids), MAX_READ):
            for r in await asyncio.wait_for(s["browser"].read(node_ids[i:i + MAX_READ]), timeout=15):
                values[r["node_id"]] = r["value"]
                if r["status"] != "Good":
                    bad.append(r["node_id"])
        return values, bad

    async def read_once(self, dash: dict, node_ids: list[str]) -> dict:
        """A one-off read on the dashboard's session (e.g. an axis's fault bits when someone opens it)."""
        lock = self._locks.setdefault(dash["id"], asyncio.Lock())
        async with lock:
            s = self._s.get(dash["id"])
            try:
                s = await self._ensure(dash, s)
                values, bad = await self._read_all(s, node_ids)
            except Exception as exc:
                if s and s.get("browser"):
                    await s["browser"].disconnect()
                    s["browser"] = None
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "values": {}, "bad": []}
            return {"ok": True, "error": None, "values": values, "bad": bad}

    async def _drop_idle(self, keep: str) -> None:
        now = time.time()
        for did, s in list(self._s.items()):
            if did != keep and now - s.get("used", 0) > IDLE_S:
                self._s.pop(did, None)
                if s.get("browser"):
                    await s["browser"].disconnect()

    async def forget(self, dash_id: str) -> None:
        s = self._s.pop(dash_id, None)
        if s and s.get("browser"):
            await s["browser"].disconnect()

    async def close(self) -> None:
        for did in list(self._s):
            await self.forget(did)


def visible_node_ids(dash: dict, all_ids: list[str]) -> list[str]:
    hidden = set(dash.get("overrides", {}).get("hidden", []))
    return [n for n in all_ids if n not in hidden]


"""Maintenance service items: a counter, an interval, and a log of each time the work was done.

An item counts up from its last service. What it counts:

- ``feet``: feet of parts made on a TSC line (completed parts x length);
- ``pieces``: pieces made on a TSC line (each one is a shear cut);
- ``strokes``: punch strokes at one station of a TSC line, for one tool type or all of them;
- ``prod_hours``: hours the TSC line spent making parts (start to end of each completed part);
- ``run_hours``: hours the machine's PLC said it was running, measured by Ghost Map's collector;
- ``online_hours``: hours Ghost Map could read the machine at all;
- ``days``: calendar days.

Items are saved in ``<data_dir>/maintenance.json``. Marking an item serviced appends to its log (who, when, a
note and the reading at the time) and starts the count again. Nothing here talks to a machine.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

KINDS = {
    "feet": {"label": "Feet made", "unit": "ft", "source": "tsc"},
    "pieces": {"label": "Pieces made (shear cuts)", "unit": "pcs", "source": "tsc"},
    "strokes": {"label": "Punch strokes", "unit": "strokes", "source": "tsc"},
    "prod_hours": {"label": "Production hours (TSC)", "unit": "h", "source": "tsc"},
    "run_hours": {"label": "Run hours (PLC running)", "unit": "h", "source": "plc"},
    "online_hours": {"label": "Powered-on hours (PLC readable)", "unit": "h", "source": "plc"},
    "days": {"label": "Calendar days", "unit": "days", "source": "clock"},
}
METER_OF = {"run_hours": "run_s", "online_hours": "online_s"}
LOG_KEPT = 200
OK, SOON, OVERDUE = "ok", "soon", "overdue"


def due_state(value: float, interval: float, warn_pct: float) -> str:
    if value >= interval:
        return OVERDUE
    if value >= interval * warn_pct / 100:
        return SOON
    return OK


class MaintenanceStore:
    def __init__(self, root: Path):
        self.path = Path(root) / "maintenance.json"
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8")).get("items", [])

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"maintenance.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps({"version": 1, "items": items}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        with self._lock:
            return self._load()

    def get(self, item_id: str) -> dict:
        for it in self.list():
            if it["id"] == item_id:
                return it
        raise KeyError(item_id)

    @staticmethod
    def check(it: dict) -> None:
        if not str(it.get("name") or "").strip():
            raise ValueError("name is required")
        if it.get("kind") not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if not float(it.get("interval") or 0) > 0:
            raise ValueError("interval must be more than 0")
        if not 1 <= float(it.get("warn_pct") or 0) <= 100:
            raise ValueError("warn at must be 1 to 100 %")
        src = KINDS[it["kind"]]["source"]
        if src == "tsc" and it.get("line") is None:
            raise ValueError("pick the TSC line")
        if it["kind"] == "strokes" and not it.get("station"):
            raise ValueError("pick the station the punch is on")
        if src == "plc" and not it.get("dash"):
            raise ValueError("pick the machine dashboard")

    def add(self, data: dict, by: Optional[str], baseline: float = 0.0) -> dict:
        now = time.time()
        it = {"id": secrets.token_hex(6), "name": str(data.get("name") or "").strip(), "kind": data.get("kind"),
              "line": data.get("line"), "station": data.get("station"), "tool": str(data.get("tool") or "").strip(),
              "dash": data.get("dash") or "", "interval": float(data.get("interval") or 0),
              "warn_pct": float(data.get("warn_pct") or 90), "notes": str(data.get("notes") or "").strip(),
              "offset": float(data.get("offset") or 0), "created": now, "created_by": by,
              "last_service": float(data.get("last_service") or now), "baseline": baseline, "log": []}
        self.check(it)
        with self._lock:
            items = self._load()
            items.append(it)
            self._save(items)
        return it

    def update(self, item_id: str, data: dict) -> dict:
        with self._lock:
            items = self._load()
            it = next((x for x in items if x["id"] == item_id), None)
            if it is None:
                raise KeyError(item_id)
            new = {**it, **{k: data[k] for k in ("name", "interval", "warn_pct", "notes", "tool") if data.get(k) is not None}}
            new["interval"], new["warn_pct"] = float(new["interval"]), float(new["warn_pct"])
            self.check(new)
            items[items.index(it)] = new
            self._save(items)
            return new

    def service(self, item_id: str, by: Optional[str], note: str, reading: Optional[float], baseline: float) -> dict:
        """Record the work done: log it with the reading at the time, and start counting again."""
        with self._lock:
            items = self._load()
            it = next((x for x in items if x["id"] == item_id), None)
            if it is None:
                raise KeyError(item_id)
            now = time.time()
            it["log"] = ([{"at": now, "by": by, "note": note.strip(), "reading": reading}] + it.get("log", []))[:LOG_KEPT]
            it["last_service"], it["baseline"], it["offset"] = now, baseline, 0.0
            self._save(items)
            return it

    def delete(self, item_id: str) -> None:
        with self._lock:
            items = self._load()
            keep = [x for x in items if x["id"] != item_id]
            if len(keep) == len(items):
                raise KeyError(item_id)
            self._save(keep)


def view(it: dict, value: Optional[float], error: Optional[str] = None) -> dict:
    """An item as the page shows it: the count since service, how far through the interval, and its state."""
    k = KINDS[it["kind"]]
    out = {**it, "unit": k["unit"], "kind_label": k["label"], "value": None, "pct": None, "state": None,
           "remaining": None, "error": error}
    if value is not None:
        v = max(value + float(it.get("offset") or 0), 0.0)
        out.update(value=round(v, 1), pct=round(v / it["interval"] * 100, 1), remaining=round(it["interval"] - v, 1),
                   state=due_state(v, it["interval"], it["warn_pct"]))
    return out

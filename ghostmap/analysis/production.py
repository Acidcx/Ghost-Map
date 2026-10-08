"""Production summary from the TSC part schedule (pure functions, no I/O).

Works on rows of ``DataView.vPartScheduleCommon``. A part is *done* when it has an end time, *running* when
it has a start time and no end, *held* when its status description says hold, and *queued* otherwise. TSC
writes 1900-01-01 for "no time yet"; those become ``None`` here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

UNSET_BEFORE = datetime(1901, 1, 1)
FEET_PER = {"in": 1 / 12, "ft": 1.0, "mm": 1 / 304.8}
HOURS_SHOWN = 12
QUEUE_SHOWN = 10
RECENT_SHOWN = 15


def _time(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return None if v < UNSET_BEFORE else v.replace(tzinfo=None)
    return None


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ts(t: Optional[datetime]) -> Optional[float]:
    return t.timestamp() if t else None  # TSC times are the plant's local time, like the HMI's clock


def normalize(row: dict) -> dict:
    r = dict(row)
    r["StartTime"], r["EndTime"] = _time(r.get("StartTime")), _time(r.get("EndTime"))
    r["IsScrap"], r["IsForcedRemake"] = bool(r.get("IsScrap")), bool(r.get("IsForcedRemake"))
    return r


def state(row: dict) -> str:
    if _time(row.get("EndTime")):
        return "done"
    if _time(row.get("StartTime")):
        return "running"
    if "hold" in str(row.get("StatusDescription") or "").lower():
        return "held"
    return "queued"


def feet(row: dict, unit: str = "in", qty_field: str = "ActualQuantity") -> float:
    return _num(row.get("Length")) * _num(row.get(qty_field)) * FEET_PER.get(unit, 1 / 12)


def shift_start(now: datetime, shifts: list[tuple[int, int]]) -> datetime:
    """The latest shift start at or before ``now`` (shifts: sorted (hour, minute) start times)."""
    best = None
    for day in (now.date() - timedelta(days=1), now.date()):
        for h, m in shifts:
            t = datetime(day.year, day.month, day.day, h, m)
            if t <= now and (best is None or t > best):
                best = t
    return best


def part_view(r: dict, unit: str, now: Optional[datetime] = None) -> dict:
    """The fields the page shows for one part."""
    req = _num(r.get("RequestedQuantity")) + _num(r.get("QuantityAdjust"))
    act = _num(r.get("ActualQuantity"))
    out = {"id": str(r.get("PartID") or ""), "order": str(r.get("OrderNumber") or ""), "state": state(r),
           "status": r.get("StatusDescription") or "", "profile": r.get("ProfileName") or r.get("StandardPartName") or "",
           "part": r.get("StandardPartName") or "", "color": r.get("Color") or "", "gauge": r.get("Thickness"),
           "length": r.get("Length"), "bundle": r.get("BundleMark") or "", "piece_mark": r.get("PieceMark") or "",
           "requested": req, "actual": act,
           "remaining": _num(r.get("QuantityRemaining")) if r.get("QuantityRemaining") is not None else max(req - act, 0),
           "feet": round(feet(r, unit, "RequestedQuantity"), 1), "start": _ts(r.get("StartTime")),
           "end": _ts(r.get("EndTime")), "scrap": r["IsScrap"], "remake": bool(_num(r.get("RemakeCode"))) or r["IsForcedRemake"],
           "error": r.get("ErrorCodeDescription") if _num(r.get("ErrorCode")) else ""}
    if out["state"] == "running" and now and r.get("StartTime"):
        elapsed = (now - r["StartTime"]).total_seconds()
        out["progress"] = min(act / req, 1.0) if req else 0.0
        out["elapsed_s"] = max(int(elapsed), 0)
        # A rough finish estimate from the pace so far.
        out["eta_s"] = int(elapsed / act * (req - act)) if act > 0 and req > act else None
    return out


def summary(open_rows: Iterable[dict], done_rows: Iterable[dict], now: datetime, shifts: list[tuple[int, int]],
            unit: str = "in") -> dict:
    """Everything the Production page shows for one line.

    ``open_rows``: the line's parts without an end time, in schedule order. ``done_rows``: its parts finished
    since at least ``min(shift start, now - 12 h)``."""
    opened = [normalize(r) for r in open_rows]
    done = [r for r in (normalize(r) for r in done_rows) if r["EndTime"]]
    start = shift_start(now, shifts)
    running = [r for r in opened if state(r) == "running"]
    queued = [r for r in opened if state(r) == "queued"]
    held = [r for r in opened if state(r) == "held"]

    shift = [r for r in done if r["EndTime"] >= start]
    hours = max((now - start).total_seconds() / 3600, 1 / 60)
    pieces = sum(_num(r.get("ActualQuantity")) for r in shift)
    ft = sum(feet(r, unit) for r in shift)
    totals = {"parts": len(shift), "pieces": pieces, "feet": round(ft, 1),
              "scrap": sum(1 for r in shift if r["IsScrap"]),
              "remakes": sum(1 for r in shift if _num(r.get("RemakeCode")) or r["IsForcedRemake"]),
              "pieces_per_hour": round(pieces / hours, 1), "feet_per_hour": round(ft / hours, 1)}

    # Feet and pieces per clock hour, for the bar chart.
    top = now.replace(minute=0, second=0, microsecond=0)
    buckets = [{"start": _ts(top - timedelta(hours=i)), "feet": 0.0, "pieces": 0.0, "parts": 0}
               for i in range(HOURS_SHOWN - 1, -1, -1)]
    first = top - timedelta(hours=HOURS_SHOWN - 1)
    for r in done:
        if r["EndTime"] >= first:
            i = int((r["EndTime"] - first).total_seconds() // 3600)
            if 0 <= i < HOURS_SHOWN:
                b = buckets[i]
                b["feet"] += feet(r, unit)
                b["pieces"] += _num(r.get("ActualQuantity"))
                b["parts"] += 1
    for b in buckets:
        b["feet"] = round(b["feet"], 1)

    # Orders still on the schedule, in the order they will run: open parts and pieces, and parts done lately.
    orders: dict[str, dict] = {}
    for r in running + queued + held:
        o = orders.setdefault(str(r.get("OrderNumber") or ""), {"order": str(r.get("OrderNumber") or ""), "open_parts": 0,
                                                                   "open_pieces": 0.0, "open_feet": 0.0, "done_parts": 0,
                                                                   "held": 0, "running": False})
        o["open_parts"] += 1
        o["open_pieces"] += max(_num(r.get("QuantityRemaining")), 0)
        o["open_feet"] += _num(r.get("Length")) * max(_num(r.get("QuantityRemaining")), 0) * FEET_PER.get(unit, 1 / 12)
        o["held"] += state(r) == "held"
        o["running"] = o["running"] or state(r) == "running"
    for r in done:
        o = orders.get(str(r.get("OrderNumber") or ""))
        if o:
            o["done_parts"] += 1
    for o in orders.values():
        o["open_feet"] = round(o["open_feet"], 1)

    recent = sorted(done, key=lambda r: r["EndTime"], reverse=True)[:RECENT_SHOWN]
    return {"at": _ts(now), "shift_start": _ts(start), "unit": unit, "totals": totals,
            "running": [part_view(r, unit, now) for r in running],
            "queue": [part_view(r, unit) for r in queued[:QUEUE_SHOWN]],
            "queue_total": {"parts": len(queued), "pieces": sum(_num(r.get("QuantityRemaining")) for r in queued),
                            "feet": round(sum(feet(r, unit, "QuantityRemaining") for r in queued), 1)},
            "held": [part_view(r, unit) for r in held],
            "hourly": buckets, "orders": list(orders.values())[:12],
            "recent": [part_view(r, unit) for r in recent]}

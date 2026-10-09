"""Production summary from the TSC part schedule (pure functions, no I/O).

TSC's words: an **order** holds jobs, a job holds **bundles**, a bundle holds **parts** (one line of the cut
list: a part number, length and quantity), and a part is made as **pieces**. Each part goes through one or
more **stations** (separate machines on the line; a part that skips a station has no row for it).

Part status (from TSC's views): 0 import, 1 pending, 2 queued, 3 hold, 4 in progress, 5 complete, 6 error.
TSC writes 1900-01-01 for "no time yet"; those become ``None`` here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

UNSET_BEFORE = datetime(1901, 1, 1)
FEET_PER = {"in": 1 / 12, "ft": 1.0, "mm": 1 / 304.8}
HOURS_SHOWN = 12
QUEUE_SHOWN = 15
RECENT_SHOWN = 15
DONE, RUNNING, HELD, QUEUED, PENDING, ERROR = "done", "running", "held", "queued", "pending", "error"


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
    if r.get("Quantity") is None:
        r["Quantity"] = _num(r.get("RequestedQuantity")) + _num(r.get("QuantityAdjust"))
    return r


def state(row: dict) -> str:
    st = row.get("Status")
    text = str(row.get("StatusDescription") or "").lower()
    if st == 5 or _time(row.get("EndTime")):
        return DONE
    if st == 4 or (st is None and _time(row.get("StartTime"))):
        return RUNNING
    if st == 3 or "hold" in text or "held" in text:
        return HELD
    if st == 6:
        return ERROR
    if st in (0, 1):
        return PENDING
    return QUEUED


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


_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def current_shift(now: datetime, rows: list[dict]) -> Optional[dict]:
    """The shift from TSC's calendar (dbo.Shift) that ``now`` falls in: {name, start, end}, or None.

    A shift belongs to the day it starts on and may run past midnight. Days are matched by the DayOfWeek
    name when there is one, else DayNumber is read as SQL Server's default (1 = Sunday)."""
    best = None
    for r in rows:
        s, e = r.get("StartTime"), r.get("EndTime")
        if not isinstance(s, datetime) or not isinstance(e, datetime):
            continue
        name = str(r.get("DayName") or "")[:3].lower()
        wd = _WEEKDAY.get(name, (int(r.get("DayNumber") or 1) + 5) % 7)
        for back in (0, 1):
            day = now.date() - timedelta(days=back)
            if day.weekday() != wd:
                continue
            start = datetime(day.year, day.month, day.day, s.hour, s.minute)
            end = datetime(day.year, day.month, day.day, e.hour, e.minute)
            if end <= start:
                end += timedelta(days=1)
            if start <= now < end and (best is None or start > best["start"]):
                best = {"name": str(r.get("Name") or ""), "start": start, "end": end}
    return best


def stop_window(stops: Iterable[dict], since: datetime, now: datetime) -> list[dict]:
    """Downtime rows (StopTime, StartTime = restart, 1900 = still stopped) clipped to [since, now]."""
    out = []
    for d in stops:
        stop, restart = _time(d.get("StopTime")), _time(d.get("StartTime"))
        if not stop:
            continue
        end = restart or now
        if end < since or stop > now:
            continue
        a, b = max(stop, since), min(end, now)
        out.append({"stop": _ts(stop), "restart": _ts(restart), "ongoing": restart is None,
                    "seconds": max(int((b - a).total_seconds()), 0), "code": d.get("StopCode"),
                    "reason": str(d.get("Description") or f"Code {d.get('StopCode')}")})
    return sorted(out, key=lambda x: x["stop"], reverse=True)


def part_view(r: dict, unit: str, now: Optional[datetime] = None, stations: Optional[list] = None) -> dict:
    """The fields the page shows for one part."""
    qty = _num(r.get("Quantity"))
    act = _num(r.get("ActualQuantity"))
    out = {"id": str(r.get("PartID") or ""), "order": str(r.get("OrderNumber") or ""), "state": state(r),
           "status": r.get("StatusDescription") or "", "queue": r.get("QueueIndex"),
           "profile": r.get("ProfileName") or r.get("StandardPartName") or "", "part": r.get("StandardPartName") or "",
           "color": r.get("Color") or "", "gauge": r.get("Thickness"), "coil": r.get("RequestedCoil") or "",
           "length": r.get("Length"), "bundle": r.get("BundleMark") or "", "piece_mark": r.get("PieceMark") or "",
           "quantity": qty, "actual": act,
           "remaining": _num(r.get("QuantityRemaining")) if r.get("QuantityRemaining") is not None else max(qty - act, 0),
           "feet": round(feet(r, unit, "Quantity"), 1), "start": _ts(r.get("StartTime")),
           "end": _ts(r.get("EndTime")), "scrap": r["IsScrap"], "remake": bool(_num(r.get("RemakeCode"))) or r["IsForcedRemake"],
           "error": r.get("ErrorCodeDescription") if _num(r.get("ErrorCode")) else ""}
    if stations is not None:
        out["stations"] = stations
    if out["state"] == RUNNING and now and r.get("StartTime"):
        elapsed = (now - r["StartTime"]).total_seconds()
        out["progress"] = min(act / qty, 1.0) if qty else 0.0
        out["elapsed_s"] = max(int(elapsed), 0)
        out["eta_s"] = int(elapsed / act * (qty - act)) if act > 0 and qty > act else None  # from the pace so far
    return out


def station_progress(rows: list[dict], station_parts: Optional[list[dict]]) -> dict:
    """Per part: {station id: {qty, in_progress, completed}}. From dbo.StationPart when readable, else from
    the view's Station1/Station2 columns ('IP', 'C' or 'x'; empty when the part skips the station)."""
    out: dict = {}
    if station_parts is not None:
        for sp in station_parts:
            out.setdefault(str(sp["PartID"]), {})[int(sp["StationID"])] = {
                "qty": _num(sp.get("QuantityCompleted")), "in_progress": bool(sp.get("IsInProgress")),
                "completed": bool(sp.get("IsCompleted"))}
        return out
    for r in rows:
        for n in (1, 2):
            code = r.get(f"Station{n}Status")
            if code:
                out.setdefault(str(r.get("PartID")), {})[n] = {"qty": _num(r.get(f"Station{n}ActualQuantity")),
                                                               "in_progress": code == "IP", "completed": code == "C"}
    return out


def summary(open_rows: Iterable[dict], done_rows: Iterable[dict], now: datetime, shift: dict, unit: str = "in",
            stations: Optional[list[dict]] = None, station_parts: Optional[list[dict]] = None,
            links: Optional[list[dict]] = None, stops: Optional[list[dict]] = None,
            orders: Optional[list[dict]] = None) -> dict:
    """Everything the Production page shows for one line.

    ``open_rows``: the line's queue (queued, held, in progress) in run order. ``done_rows``: parts finished since
    at least ``min(shift start, now - 12 h)``. ``shift``: {name, start, end?}. ``stations``: this line's stations
    (dbo.Station) or None; ``station_parts``: dbo.StationPart rows or None (then the view's Station1/2 columns are
    used); ``links``: station dependencies; ``stops``: downtime rows or None when not readable; ``orders``:
    whole-order totals for the orders in the queue."""
    opened = [normalize(r) for r in open_rows]
    done = [r for r in (normalize(r) for r in done_rows) if r["EndTime"]]
    start = shift["start"]
    running = [r for r in opened if state(r) == RUNNING]
    queued = [r for r in opened if state(r) == QUEUED]
    held = [r for r in opened if state(r) == HELD]
    prog = station_progress(opened + done, station_parts)

    # ---- this shift
    shift_done = [r for r in done if r["EndTime"] >= start]
    hours = max((now - start).total_seconds() / 3600, 1 / 60)
    pieces = sum(_num(r.get("ActualQuantity")) for r in shift_done)
    ft = sum(feet(r, unit) for r in shift_done)
    worked = {r.get("OrderNumber") for r in shift_done} | {r.get("OrderNumber") for r in running}
    totals = {"orders": len(worked - {None}), "parts": len(shift_done), "pieces": pieces, "feet": round(ft, 1),
              "scrap_pieces": sum(_num(r.get("ActualQuantity")) for r in shift_done if r["IsScrap"]),
              "remakes": sum(1 for r in shift_done if _num(r.get("RemakeCode")) or r["IsForcedRemake"]),
              "pieces_per_hour": round(pieces / hours, 1), "feet_per_hour": round(ft / hours, 1)}
    down = None
    if stops is not None:
        in_shift = stop_window(stops, start, now)
        secs = sum(s["seconds"] for s in in_shift)
        reasons: dict = {}
        for s in in_shift:
            x = reasons.setdefault(s["reason"], {"reason": s["reason"], "stops": 0, "seconds": 0})
            x["stops"] += 1
            x["seconds"] += s["seconds"]
        elapsed = max((now - start).total_seconds(), 1)
        down = {"stops": in_shift[:20], "count": len(in_shift), "seconds": secs, "ongoing": any(s["ongoing"] for s in in_shift),
                "uptime": round(max(elapsed - secs, 0) / elapsed, 3),
                "reasons": sorted(reasons.values(), key=lambda x: -x["seconds"])}

    # ---- stations: what each one is doing now
    line_stations = []
    if stations:
        waits: dict = {}
        names = {int(s["StationID"]): str(s.get("Name") or f"Station {s['StationID']}") for s in stations}
        for ln in links or []:
            waits.setdefault(int(ln["DependentStationID"]), []).append(int(ln["IndependentStationID"]))
        sids = [int(s["StationID"]) for s in stations]
    else:  # no station table: the stations the view shows, if any part on this line uses them
        sids = sorted({sid for r in opened + done for sid in prog.get(str(r.get("PartID")), {})})
        names, waits = {s: f"Station {s}" for s in sids}, {}
    for sid in sids:
        cur = next((r for r in opened if prog.get(str(r.get("PartID")), {}).get(sid, {}).get("in_progress")), None)
        if cur is None:  # the running part may already be past this station, or not yet at it
            cur = next((r for r in running if sid in prog.get(str(r.get("PartID")), {})), None)
        p = prog.get(str(cur.get("PartID")), {}).get(sid) if cur else None
        qty = _num(cur.get("Quantity")) if cur else 0
        line_stations.append({"id": sid, "name": names.get(sid, f"Station {sid}"),
                              "waits_for": [names.get(w, f"Station {w}") for w in waits.get(sid, [])],
                              "state": "running" if p and p["in_progress"] else "done" if p and p["completed"] else "idle",
                              "order": str(cur.get("OrderNumber")) if cur else None,
                              "profile": (cur.get("ProfileName") or "") if cur else "", "length": cur.get("Length") if cur else None,
                              "qty": p["qty"] if p else 0, "of": qty,
                              "progress": min(p["qty"] / qty, 1.0) if p and qty else 0.0})

    def stations_of(r):
        return [{"id": sid, "name": names.get(sid, f"Station {sid}"), **v} for sid, v in sorted(prog.get(str(r.get("PartID")), {}).items())]

    # ---- feet and pieces per clock hour
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

    # ---- orders still on the schedule, in run order, with whole-order totals when known
    totals_by = {str(o["OrderNumber"]): o for o in orders or []}
    order_list: dict = {}
    for r in running + queued + held:
        key = str(r.get("OrderNumber") or "")
        o = order_list.setdefault(key, {"order": key, "queued_parts": 0, "held": 0, "running": False})
        o["queued_parts"] += state(r) != RUNNING
        o["held"] += state(r) == HELD
        o["running"] = o["running"] or state(r) == RUNNING
    for key, o in order_list.items():
        t = totals_by.get(key)
        if t:
            o.update(parts=int(_num(t.get("Parts"))), parts_done=int(_num(t.get("PartsDone"))),
                     pieces=_num(t.get("Pieces")), pieces_done=_num(t.get("PiecesDone")),
                     bundles=int(_num(t.get("Bundles"))),
                     feet_left=round(_num(t.get("LengthLeft")) * FEET_PER.get(unit, 1 / 12), 1))

    q = [r for r in opened if state(r) in (QUEUED, HELD)]  # run order
    q_orders = {r.get("OrderNumber") for r in q}
    q_bundles = {(r.get("OrderNumber"), r.get("BundleMark")) for r in q}
    recent = sorted(done, key=lambda r: r["EndTime"], reverse=True)[:RECENT_SHOWN]
    return {"at": _ts(now), "shift": {"name": shift.get("name") or "", "start": _ts(start), "end": _ts(shift.get("end")),
                                      "source": shift.get("source", "settings")},
            "unit": unit, "totals": totals, "downtime": down, "stations": line_stations,
            "running": [part_view(r, unit, now, stations_of(r)) for r in running],
            "queue": [part_view(r, unit, stations=stations_of(r)) for r in q[:QUEUE_SHOWN]],
            "queue_total": {"orders": len(q_orders), "bundles": len(q_bundles), "parts": len(q),
                            "pieces": sum(_num(r.get("QuantityRemaining")) for r in q),
                            "feet": round(sum(feet(r, unit, "QuantityRemaining") for r in q), 1)},
            "held": [part_view(r, unit) for r in held],
            "hourly": buckets, "orders": list(order_list.values())[:15],
            "recent": [part_view(r, unit) for r in recent]}

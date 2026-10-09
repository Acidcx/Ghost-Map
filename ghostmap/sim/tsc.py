"""A simulated TSC database for the demo and tests: the part schedule views plus the station, shift, downtime
and tooling tables Ghost Map reads.

Made-up orders on two lines, shaped like the real tables:

- Line 1, a cut-to-length panel line: station 1 punches and rollforms every part; station 2, a notcher, is
  only used by some parts (a skipped station has no StationPart row, as in TSC).
- Line 2, a garage door line: the pan (station 3) and the back skin (station 4) run side by side and are
  joined at station 5, which depends on both.

The schedule is worked out from the clock, so the demo moves on its own (running parts count up, finish,
and the next one starts) without storing anything. Status codes follow the TSC views: 0 import, 1 pending,
2 queued, 3 hold, 4 in progress, 5 complete, 6 error.
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timedelta

UNSET = datetime(1900, 1, 1)
LINES = [(1, "Line 1"), (2, "Line 2")]
STATUS_NAMES = {0: "Import", 1: "Pending", 2: "Queued", 3: "Hold", 4: "In Progress", 5: "Complete", 6: "Error"}
STATIONS = [  # StationID, LineID, Name, Description
    (1, 1, "Punch / Rollformer", "Pre-punch press and rollformer"),
    (2, 1, "Notcher", "End notcher (some parts only)"),
    (3, 2, "Pan", "Front skin rollformer"),
    (4, 2, "Back Skin", "Back skin / style cover rollformer"),
    (5, 2, "Sandwich", "Joins the pan and back skin"),
]
DEPENDENCY_TYPES = {1: "Requires"}
DEPENDENCIES = [(5, 3, 1), (5, 4, 1)]  # dependent station, independent station, type
LINE_STATIONS = {1: [1, 2], 2: [3, 4, 5]}
DAY_NAMES = {1: "Sunday", 2: "Monday", 3: "Tuesday", 4: "Wednesday", 5: "Thursday", 6: "Friday", 7: "Saturday"}
SHIFTS = [("1st", 1, (6, 0), (14, 0)), ("2nd", 2, (14, 0), (22, 0)), ("3rd", 3, (22, 0), (6, 0))]
STOP_CODES = {1: "Coil change", 2: "Material jam", 3: "Tooling change", 4: "Break", 5: "Maintenance", 6: "Waiting on material"}
TOOL_TYPES = {"RD50": '0.50" round', "SL38": '3/8" x 1" slot', "RD25": '0.25" round', "N90": "90 deg notch",
              "N45": "45 deg corner notch", "LK": "Lock tab"}
PATTERNS = {  # name -> [(ToolType, StationID)], the holes one hit of the pattern makes
    "2 Row Vertical": [("RD50", 1), ("RD50", 1)],
    "4 Row Vertical": [("RD50", 1)] * 4,
    "9 Hole Vert Stag": [("RD25", 1)] * 9,
    "Slot Pair": [("SL38", 1), ("SL38", 1)],
    "End Notch": [("N90", 2), ("N90", 2)],
    "Pan Lock": [("LK", 3), ("LK", 3)],
    "Skin Corner": [("N45", 4)] * 4,
}
PROFILES = {1: [("R-Panel", 0.019, 41.25, 36.0), ("PBR", 0.024, 41.25, 36.0), ("Soffit", 0.019, 18.0, 12.0)],
            2: [("Door Pan 24", 0.019, 24.0, 21.0), ("Door Pan 21", 0.019, 21.0, 18.0)]}
CUSTOMERS = ["Demo Builders", "Sample Steel", "Example Barns", "Test Structures"]
FEET_PER_MIN = {1: 110.0, 2: 45.0}
HISTORY_H = 14
SIM_AGE_DAYS = 400


def _part(rng: random.Random, line: tuple, order: int, bundle: int, idx: int, now: datetime) -> dict:
    prof, gauge, strip, web = rng.choice(PROFILES[line[0]])
    qty = rng.choice([8, 12, 20, 24, 40, 60] if line[0] == 1 else [4, 6, 8, 12])
    length = rng.choice([96, 120, 144, 168, 204, 240, 288] if line[0] == 1 else [96, 108, 120, 144, 192])
    return {"PartID": str(uuid.UUID(int=rng.getrandbits(128))), "OrderNumber": str(order), "Status": 2,
            "StatusDescription": STATUS_NAMES[2], "RequestedQuantity": qty, "QuantityAdjust": 0, "ActualQuantity": 0,
            "QuantityRemaining": qty, "Quantity": qty, "StandardPartName": prof, "ProfileName": prof,
            "Thickness": gauge, "StripWidth": strip, "WebWidth": web, "Tooling": "Standard", "RequestedCoil": f"C{rng.randint(100, 999)}",
            "Color": rng.choice(["Galvalume", "Polar White", "Burgundy"]), "BundleID": f"b{line[0]}-{bundle}",
            "BundleMark": f"BM{bundle % 50 + 1}", "Length": length, "PieceMark": str(idx % 12 + 1), "RecordXofY": "1 of 1",
            "IsEndOfBundle": idx % 4 == 3, "ErrorCode": 0, "ErrorCodeDescription": "No Error", "RemakeCode": 0,
            "IsScrap": False, "IsForcedRemake": False, "PriorityIndex": idx, "ImportIndex": 1000 + idx,
            "EntryDate": now - timedelta(days=1), "StartTime": UNSET, "EndTime": UNSET, "LineID": line[0],
            "LineName": line[1], "CustomerName": rng.choice(CUSTOMERS), "Station1Status": None, "Station1ActualQuantity": None,
            "Station2Status": None, "Station2ActualQuantity": None}


def _features(rng: random.Random, p: dict) -> tuple[list, list, list]:
    """Patterns (PartPattern rows), one-off holes and notches for a part, and the stations it visits."""
    pats, holes = [], []
    if p["LineID"] == 1:
        pats.append({"PatternName": "2 Row Vertical", "LeadOffset": 0.0, "IsRepeat": False, "RepeatOffset": 0.0, "EndOffset": 0.0})
        pats.append({"PatternName": rng.choice(["4 Row Vertical", "9 Hole Vert Stag"]), "LeadOffset": 2.0, "IsRepeat": True,
                     "RepeatOffset": 24.0, "EndOffset": 2.0})
        if rng.random() < 0.3:
            holes.append({"ToolType": "SL38", "StationID": 1})
        stations = [1]
        if rng.random() < 0.4:  # this part needs the notcher
            pats.append({"PatternName": "End Notch", "LeadOffset": 0.0, "IsRepeat": False, "RepeatOffset": 0.0, "EndOffset": 0.0})
            stations.append(2)
    else:
        pats.append({"PatternName": "Pan Lock", "LeadOffset": 3.0, "IsRepeat": True, "RepeatOffset": 18.0, "EndOffset": 3.0})
        pats.append({"PatternName": "Skin Corner", "LeadOffset": 0.0, "IsRepeat": False, "RepeatOffset": 0.0, "EndOffset": 0.0})
        stations = [3, 4, 5]
    return pats, holes, stations


def world(now: datetime | None = None, seed: int = 7) -> dict:
    """Every simulated row as of ``now``: about 14 hours of finished work with stops between parts, the
    running part(s), the queue (one part on hold) and a few parts imported but not queued yet."""
    now = now or datetime.now().replace(microsecond=0)
    rows, station_parts, part_patterns, holes, downtime = [], [], [], [], []
    for line in LINES:
        lid = line[0]
        rng = random.Random(seed * 100 + lid)
        t = (now - timedelta(hours=HISTORY_H)).replace(minute=0, second=0)
        order, bundle, idx, queue = 41000 + lid * 1000, 0, 0, 0
        while True:
            if idx % 6 == 0:
                order += 1
            if idx % 3 == 0:
                bundle += 1
            p = _part(rng, line, order, bundle, idx, now)
            pats, hs, stations = _features(rng, p)
            if idx % 12 == 11:  # a stop between parts, with a reason code
                code = rng.choice(list(STOP_CODES))
                stop = t + timedelta(minutes=rng.uniform(0.5, 2))
                restart = stop + timedelta(minutes=rng.uniform(3, 12))
                downtime.append({"LineID": lid, "StopTime": stop, "StartTime": restart if restart <= now else UNSET,
                                 "StopCode": code, "Description": STOP_CODES[code]})
                if restart > now:
                    break  # stopped right now: nothing running
                t = restart
            run_min = p["RequestedQuantity"] * p["Length"] / 12 / FEET_PER_MIN[lid] + rng.uniform(1.5, 4)
            start = t + timedelta(minutes=rng.uniform(0.2, 0.8))
            end = start + timedelta(minutes=run_min)
            if start > now:
                break
            done = end <= now
            frac = 1.0 if done else (now - start) / (end - start)
            made = p["RequestedQuantity"] if done else int(p["RequestedQuantity"] * frac)
            p.update(StartTime=start, ActualQuantity=made, QuantityRemaining=p["RequestedQuantity"] - made)
            if done:
                p.update(EndTime=end, Status=5, StatusDescription=STATUS_NAMES[5])
                if rng.random() < 0.06:
                    p.update(IsScrap=True, RemakeCode=2, ErrorCode=14, ErrorCodeDescription="Bad cut length")
            else:
                p.update(Status=4, StatusDescription=STATUS_NAMES[4], QueueIndex=queue)
                queue += 1
            for i, sid in enumerate(stations):
                # Side-by-side stations run at slightly different paces; a joining station trails both.
                q = made if done else min(p["RequestedQuantity"], int(p["RequestedQuantity"] * frac * (1.15 - 0.2 * i)))
                station_parts.append({"StationID": sid, "PartID": p["PartID"], "QuantityCompleted": q,
                                      "IsInProgress": not done and q < p["RequestedQuantity"], "IsCompleted": done or q >= p["RequestedQuantity"]})
            rows.append(p)
            part_patterns += [{**x, "PartID": p["PartID"]} for x in pats]
            holes += [{**x, "PartID": p["PartID"]} for x in hs]
            idx += 1
            if not done:
                break
            t = end
        for q in range(14):  # the queue
            if (idx + q) % 6 == 0:
                order += 1
            if (idx + q) % 3 == 0:
                bundle += 1
            p = _part(rng, line, order, bundle, idx + q, now)
            pats, hs, stations = _features(rng, p)
            if q == 5:
                p["Status"], p["StatusDescription"] = 3, STATUS_NAMES[3]
            p["QueueIndex"] = queue
            queue += 1
            rows.append(p)
            part_patterns += [{**x, "PartID": p["PartID"]} for x in pats]
            holes += [{**x, "PartID": p["PartID"]} for x in hs]
            station_parts += [{"StationID": sid, "PartID": p["PartID"], "QuantityCompleted": 0, "IsInProgress": False,
                               "IsCompleted": False} for sid in stations]
        for q in range(3):  # imported for a later order, not queued yet
            p = _part(rng, line, order + 1, bundle + 1, idx + 14 + q, now)
            p["Status"], p["StatusDescription"] = 1, STATUS_NAMES[1]
            rows.append(p)
    by_part = {}
    for sp in station_parts:
        by_part.setdefault(sp["PartID"], {})[sp["StationID"]] = sp
    for r in rows:  # the view's Station1/Station2 columns, as vPartScheduleCommon builds them
        for n in (1, 2):
            sp = by_part.get(r["PartID"], {}).get(n)
            if sp:
                r[f"Station{n}Status"] = "IP" if sp["IsInProgress"] else "C" if sp["IsCompleted"] else "x"
                r[f"Station{n}ActualQuantity"] = sp["QuantityCompleted"]
    return {"rows": rows, "station_parts": station_parts, "part_patterns": part_patterns, "holes": holes,
            "downtime": downtime, "start": (now - timedelta(hours=HISTORY_H)).replace(minute=0, second=0)}


def schedule(now: datetime | None = None, seed: int = 7) -> list[dict]:
    """The rows of the simulated ``vPartScheduleCommon``."""
    return world(now, seed)["rows"]


def pattern_hits(pp: dict, length: float) -> int:
    """How many times a pattern is punched along a part (TSC's repeat pattern: lead, every repeat, end)."""
    if pp.get("IsRepeat") and float(pp.get("RepeatOffset") or 0) > 0:
        span = float(length) - float(pp.get("LeadOffset") or 0) - float(pp.get("EndOffset") or 0)
        return int(span // float(pp["RepeatOffset"])) + 1 if span >= 0 else 0
    return 1


class SimTsc:
    """Answers the same questions as the SQL source (``collectors/tsc.py``), from ``world()``."""

    name = "demo"

    def _w(self) -> dict:
        return world()

    def lines(self) -> list[dict]:
        return [{"LineID": i, "LineName": n} for i, n in LINES]

    def open_parts(self, line_id: int, limit: int = 300) -> list[dict]:
        rows = [r for r in self._w()["rows"] if r["LineID"] == line_id and r["Status"] in (2, 3, 4)]
        return sorted(rows, key=lambda r: r["QueueIndex"])[:limit]

    def done_parts(self, line_id: int, since: datetime, limit: int = 3000) -> list[dict]:
        rows = [r for r in self._w()["rows"] if r["LineID"] == line_id and r["Status"] == 5 and r["EndTime"] >= since]
        return sorted(rows, key=lambda r: r["EndTime"], reverse=True)[:limit]

    def order_totals(self, line_id: int, orders: list[str]) -> list[dict]:
        out = {}
        for r in self._w()["rows"]:
            if r["LineID"] == line_id and r["OrderNumber"] in orders:
                o = out.setdefault(r["OrderNumber"], {"OrderNumber": r["OrderNumber"], "Parts": 0, "PartsDone": 0, "Pieces": 0,
                                                      "PiecesDone": 0, "Bundles": set(), "LengthLeft": 0.0})
                o["Parts"] += 1
                o["PartsDone"] += r["Status"] == 5
                o["Pieces"] += r["Quantity"]
                o["PiecesDone"] += r["ActualQuantity"]
                o["Bundles"].add(r["BundleID"])
                o["LengthLeft"] += r["Length"] * max(r["Quantity"] - r["ActualQuantity"], 0)
        return [{**o, "Bundles": len(o["Bundles"])} for o in out.values()]

    def station_parts(self, part_ids: list[str]) -> list[dict]:
        ids = set(part_ids)
        return [sp for sp in self._w()["station_parts"] if sp["PartID"] in ids]

    def stations(self) -> list[dict]:
        return [{"StationID": s, "LineID": ln, "Name": n, "Description": d} for s, ln, n, d in STATIONS]

    def station_links(self) -> list[dict]:
        return [{"DependentStationID": d, "IndependentStationID": i, "TypeName": DEPENDENCY_TYPES[t]} for d, i, t in DEPENDENCIES]

    def shifts(self) -> list[dict]:
        out, sid = [], 0
        for day in range(1, 8):
            for name, num, (h1, m1), (h2, m2) in SHIFTS:
                sid += 1
                out.append({"ShiftID": sid, "Name": name, "DayNumber": day, "DayName": DAY_NAMES[day], "ShiftNumber": num,
                            "StartTime": datetime(1900, 1, 1, h1, m1), "EndTime": datetime(1900, 1, 1, h2, m2)})
        return out

    def downtime(self, line_id: int, since: datetime, limit: int = 500) -> list[dict]:
        rows = [d for d in self._w()["downtime"] if d["LineID"] == line_id and (d["StartTime"] <= UNSET or d["StartTime"] >= since)]
        return sorted(rows, key=lambda d: d["StopTime"], reverse=True)[:limit]

    def tool_types(self) -> list[dict]:
        return [{"HoleType": k, "Description": v} for k, v in TOOL_TYPES.items()]

    # ------------------------------------------------------------- counters (maintenance)
    def _scale(self, w: dict, since: datetime) -> float:
        """The sim only has ~14 hours of parts: earlier periods are extrapolated at the same rate."""
        hours = (datetime.now() - w["start"]).total_seconds() / 3600
        want = min((datetime.now() - since).total_seconds() / 3600, SIM_AGE_DAYS * 24)  # "all time": the sim line is this old
        return max(want / hours, 1.0) if hours > 0 else 1.0

    def totals(self, line_id: int, since: datetime) -> dict:
        w = self._w()
        done = [r for r in w["rows"] if r["LineID"] == line_id and r["Status"] == 5 and r["EndTime"] >= since]
        k = self._scale(w, since)
        return {"Parts": round(len(done) * k), "Pieces": round(sum(r["ActualQuantity"] for r in done) * k),
                "LengthTotal": sum(r["Length"] * r["ActualQuantity"] for r in done) * k,
                "ScrapPieces": round(sum(r["ActualQuantity"] for r in done if r["IsScrap"]) * k),
                "RunSeconds": sum((r["EndTime"] - r["StartTime"]).total_seconds() for r in done) * k,
                "FirstEnd": min((r["EndTime"] for r in done), default=None)}

    def strokes(self, line_id: int, since: datetime) -> list[dict]:
        w = self._w()
        done = {r["PartID"]: r for r in w["rows"] if r["LineID"] == line_id and r["Status"] == 5 and r["EndTime"] >= since}
        k = self._scale(w, since)
        out: dict = {}
        for pp in w["part_patterns"]:
            r = done.get(pp["PartID"])
            if r:
                for tool, sid in PATTERNS[pp["PatternName"]]:
                    out[(sid, tool)] = out.get((sid, tool), 0) + r["ActualQuantity"] * pattern_hits(pp, r["Length"])
        for h in w["holes"]:
            r = done.get(h["PartID"])
            if r:
                out[(h["StationID"], h["ToolType"])] = out.get((h["StationID"], h["ToolType"]), 0) + r["ActualQuantity"]
        return [{"StationID": s, "ToolType": t, "Strokes": round(n * k)} for (s, t), n in out.items()]


def demo_service_items() -> list[dict]:
    """Service items for the demo's Maintenance page (offsets stand for use before Ghost Map counted)."""
    day = 86400
    return [
        {"name": "Shear blade: rotate or sharpen", "kind": "pieces", "line": 1, "interval": 250000, "offset": 231000,
         "notes": "Rotate to a fresh edge; sharpen after all four are used."},
        {"name": "0.50\" round punch and die", "kind": "strokes", "line": 1, "station": 1, "tool": "RD50", "interval": 1500000,
         "offset": 620000},
        {"name": "0.25\" round punch and die", "kind": "strokes", "line": 1, "station": 1, "tool": "RD25", "interval": 1500000,
         "offset": 1540000},
        {"name": "Notcher blades", "kind": "strokes", "line": 1, "station": 2, "interval": 400000, "offset": 90000},
        {"name": "Rollformer: grease stands and chain", "kind": "feet", "line": 1, "interval": 250000, "offset": 180000},
        {"name": "Gearbox oil check", "kind": "prod_hours", "line": 1, "interval": 2000, "offset": 1650},
        {"name": "Sandwich press: check platens", "kind": "pieces", "line": 2, "interval": 50000, "offset": 12000},
        {"name": "Line 2 safety check", "kind": "days", "interval": 30, "last_service": time.time() - 26 * day},
    ]

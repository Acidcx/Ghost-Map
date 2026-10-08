"""A simulated TSC part schedule (``DataView.vPartScheduleCommon``) for the demo and tests.

Made-up orders on two lines, shaped like the real view: a day of completed parts, one running, a queue
and a held part. The schedule is worked out from the clock, so the demo moves on its own (the running part
counts up, finishes, and the next one starts) without storing anything.

The status codes and descriptions here are a guess at TSC's; Ghost Map does not rely on them. It reads a
part as done when it has an end time, running when it has a start time and no end, and held when its
status description says so.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta

UNSET = datetime(1900, 1, 1)
LINES = [(1, "Line 1"), (2, "Line 2")]
PROFILES = [("R-Panel", 0.019, 41.25, 36.0), ("PBR", 0.024, 41.25, 36.0), ("Purlin 8Z", 0.075, 14.5, 8.0),
            ("Soffit", 0.019, 18.0, 12.0), ("Trim 4in", 0.024, 12.0, 4.0)]
CUSTOMERS = ["Demo Builders", "Sample Steel", "Example Barns", "Test Structures"]
FEET_PER_MIN = 110.0
STATUS = {"queued": (2, "Queued"), "running": (3, "Running"), "done": (5, "Complete"), "held": (4, "On Hold")}


def _part(rng: random.Random, line: tuple, order: int, idx: int, now: datetime) -> dict:
    prof, gauge, strip, web = rng.choice(PROFILES)
    qty = rng.choice([8, 12, 20, 24, 40, 60])
    length = rng.choice([96, 120, 144, 168, 204, 240, 288])
    return {"PartID": str(uuid.UUID(int=rng.getrandbits(128))), "OrderNumber": str(order), "Status": 2,
            "StatusDescription": "Queued", "RequestedQuantity": qty, "QuantityAdjust": 0, "ActualQuantity": 0,
            "QuantityRemaining": qty, "Quantity": qty, "StandardPartName": prof, "ProfileName": prof,
            "Thickness": gauge, "StripWidth": strip, "WebWidth": web, "Tooling": "Standard", "Color": rng.choice(["Galvalume", "Polar White", "Burgundy"]),
            "BundleMark": f"BM{idx % 7 + 1}", "Length": length, "PieceMark": str(idx % 12 + 1), "RecordXofY": "1 of 1",
            "IsEndOfBundle": idx % 4 == 3, "ErrorCode": 0, "ErrorCodeDescription": "No Error", "RemakeCode": 0,
            "IsScrap": False, "IsForcedRemake": False, "PriorityIndex": idx, "ImportIndex": 1000 + idx,
            "EntryDate": now - timedelta(days=1), "StartTime": UNSET, "EndTime": UNSET, "LineID": line[0],
            "LineName": line[1], "CustomerName": rng.choice(CUSTOMERS), "Station1Status": ".", "Station1ActualQuantity": 0,
            "Station2Status": None, "Station2ActualQuantity": None}


def schedule(now: datetime | None = None, seed: int = 7) -> list[dict]:
    """Every row of the simulated view as of ``now``: about 14 hours of finished work, then the queue."""
    now = now or datetime.now().replace(microsecond=0)
    rows = []
    for line in LINES:
        rng = random.Random(seed * 100 + line[0])
        t = (now - timedelta(hours=14)).replace(minute=0, second=0)
        order, idx = 41000 + line[0] * 1000, 0
        # Parts are worked through in priority order; their times follow from length, quantity and speed.
        while True:
            if idx % 6 == 0:
                order += 1
            p = _part(rng, line, order, idx, now)
            run_min = p["RequestedQuantity"] * p["Length"] / 12 / FEET_PER_MIN + rng.uniform(1.5, 4)
            start, end = t + timedelta(minutes=rng.uniform(0.5, 6)), None
            end = start + timedelta(minutes=run_min)
            if end <= now:
                p.update(StartTime=start, EndTime=end, ActualQuantity=p["RequestedQuantity"], QuantityRemaining=0)
                p["Status"], p["StatusDescription"] = STATUS["done"]
                if rng.random() < 0.06:
                    p.update(IsScrap=True, RemakeCode=2, ErrorCode=14, ErrorCodeDescription="Bad cut length")
                rows.append(p)
                t, idx = end, idx + 1
                continue
            if start <= now:
                done = int(p["RequestedQuantity"] * (now - start) / (end - start))
                p.update(StartTime=start, ActualQuantity=done, QuantityRemaining=p["RequestedQuantity"] - done,
                         Station1ActualQuantity=done)
                p["Status"], p["StatusDescription"] = STATUS["running"]
                rows.append(p)
                idx += 1
            break
        for q in range(14):  # the queue
            if (idx + q) % 6 == 0:
                order += 1
            p = _part(rng, line, order, idx + q, now)
            if q == 5:
                p["Status"], p["StatusDescription"] = STATUS["held"]
            rows.append(p)
    return rows


class SimTsc:
    """Answers the same questions as the SQL source, from ``schedule()``."""

    name = "demo"

    def lines(self) -> list[dict]:
        return [{"LineID": i, "LineName": n} for i, n in LINES]

    def open_parts(self, line_id: int, limit: int = 300) -> list[dict]:
        rows = [r for r in schedule() if r["LineID"] == line_id and r["EndTime"] <= UNSET]
        return sorted(rows, key=lambda r: (r["PriorityIndex"], r["ImportIndex"]))[:limit]

    def done_parts(self, line_id: int, since: datetime, limit: int = 3000) -> list[dict]:
        rows = [r for r in schedule() if r["LineID"] == line_id and r["EndTime"] >= since]
        return sorted(rows, key=lambda r: r["EndTime"], reverse=True)[:limit]

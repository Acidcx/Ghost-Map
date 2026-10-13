"""Production totals kept by Ghost Map (ghostmap/prodhistory.py): copied from TSC by the hour, read by day and
week, and kept when TSC no longer has the parts."""

import asyncio
import contextlib
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from ghostmap import prodhistory
from ghostmap.collectors.tsc import TscConfig
from ghostmap.prodhistory import ProductionHistory, period_start, rollup
from ghostmap.web import tsc as web_tsc
from ghostmap.web.app import create_app
from ghostmap.web.tsc import TscService

H = {"X-Ghostmap": "1"}
NOW = datetime(2026, 10, 14, 9, 30)  # a Wednesday


def hour(h, **kw):
    return {"hour": h, "parts": 1, "pieces": 10, "length": 1200.0, "scrap_pieces": 0, "run_s": 1800.0, **kw}


def test_days_start_with_the_first_shift_and_weeks_on_monday():
    assert period_start(NOW, "day", 6) == datetime(2026, 10, 14, 6)
    assert period_start(datetime(2026, 10, 14, 3), "day", 6) == datetime(2026, 10, 13, 6)  # night shift: yesterday
    assert period_start(datetime(2026, 10, 14, 3), "day", 0) == datetime(2026, 10, 14)
    assert period_start(NOW, "week", 6) == datetime(2026, 10, 12, 6)
    assert period_start(datetime(2026, 10, 12, 5), "week", 6) == datetime(2026, 10, 5, 6)  # Monday before 06:00
    assert period_start(NOW, "hour") == datetime(2026, 10, 14, 9)

    hours = [hour(datetime(2026, 10, 14, 7)), hour(datetime(2026, 10, 14, 5), pieces=4, scrap_pieces=1),
             hour(datetime(2026, 10, 13, 6)), hour(datetime(2026, 10, 1, 8), pieces=999)]  # the last is out of range
    rows = rollup(hours, "day", 3, NOW, 6, first_hour=datetime(2026, 10, 12, 12), feet_per=1 / 12)
    assert [r["label"] for r in rows] == ["Mon 2026-10-12", "Tue 2026-10-13", "Wed 2026-10-14"]
    assert [r["pieces"] for r in rows] == [0, 14, 10] and rows[1]["scrap_pieces"] == 1
    assert rows[1]["feet"] == 200.0 and rows[1]["prod_hours"] == 1.0 and rows[1]["parts"] == 2
    assert [r["now"] for r in rows] == [False, False, True]
    assert [r["partial"] for r in rows] == [True, False, False]  # began before Ghost Map's history
    assert rows[2]["start"] == datetime(2026, 10, 14, 6).timestamp()

    weeks = rollup(hours, "week", 2, NOW, 6)
    assert [w["label"] for w in weeks] == ["Week 41 (2026-10-05)", "Week 42 (2026-10-12)"] and weeks[1]["pieces"] == 24


class FakeTsc:
    """TSC's completed parts, summed by hour: ``data`` maps (line, hour) -> totals."""
    name = "sql"
    timings: list = []

    def __init__(self):
        self.data = {}
        self.asked = []
        self.fail = False

    def session(self, heavy=False):
        return contextlib.nullcontext(self)

    def lines(self):
        return [{"LineID": 1, "LineName": "Panel line"}]

    def shifts(self):
        return [{"ShiftNumber": 1, "StartTime": datetime(1900, 1, 1, 5, 0)}, {"ShiftNumber": 2, "StartTime": datetime(1900, 1, 1, 15)}]

    def hourly(self, line_id, since):
        if self.fail:
            raise ConnectionError("SQL Server not answering")
        self.asked.append(since)
        return [{"Hour": h, "Parts": 1, "Pieces": n, "LengthTotal": n * 120.0, "ScrapPieces": 0, "RunSeconds": 600}
                for (ln, h), n in sorted(self.data.items()) if ln == line_id and h >= since]


def test_sync_keeps_what_tsc_forgets(tmp_path, monkeypatch):
    fake = FakeTsc()
    monkeypatch.setattr(web_tsc, "source_for", lambda cfg: fake)
    svc = TscService(tmp_path, history=ProductionHistory(tmp_path))
    svc.cfg = TscConfig(server="sql01", database="TSC")
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    old, recent = now - timedelta(days=20), now - timedelta(hours=2)
    fake.data = {(1, old): 50, (1, recent): 30}

    assert asyncio.run(svc.sync_history()) == {1: 2}
    assert fake.asked[0] <= now - timedelta(days=prodhistory.BACKFILL_DAYS) + timedelta(hours=1)  # first sync: back-fill
    assert svc.hist_state["day_start"] == 5  # TSC's first shift

    # TSC clears its old parts and corrects a recent one; the next sync only reads back REFRESH_H hours.
    fake.data = {(1, recent): 28, (1, now): 7}
    asyncio.run(svc.sync_history())
    assert fake.asked[1] >= now - timedelta(hours=prodhistory.REFRESH_H)
    kept = {h["hour"]: h["pieces"] for h in svc.hist.hours(1, now - timedelta(days=30))}
    assert kept == {old: 50, recent: 28, now: 7}

    days = asyncio.run(svc.history(1, "day", 30))
    assert days["day_start"] == 5 and days["name"] == "Panel line" and days["sync_error"] is None
    assert sum(r["pieces"] for r in days["rows"]) == 85 and days["rows"][-1]["now"]
    assert sum(r["feet"] for r in days["rows"]) == 850.0  # inches in TSC, feet here

    # TSC down: the history still reads, and says the copy failed.
    fake.fail = True
    try:
        asyncio.run(svc.sync_history())
    except ConnectionError:
        pass
    assert svc.state["last_error"] and "not answering" in svc.hist_state["error"]
    assert sum(r["pieces"] for r in asyncio.run(svc.history(1, "week", 6))["rows"]) == 85

    # Pointed at another TSC database, the history starts again (no mixing of two plants' numbers).
    fake.fail = False
    svc.cfg = TscConfig(server="sql02", database="TSC")
    fake.data = {(1, now): 3}
    asyncio.run(svc.sync_history())
    assert {h["hour"]: h["pieces"] for h in svc.hist.hours(1, now - timedelta(days=30))} == {now: 3}
    svc.hist.close()


def test_history_api_with_the_demo_source(tmp_path):
    with TestClient(create_app(str(tmp_path), demo=True, demo_opcua=False, collect=False), headers=H) as c:
        d = c.get("/api/tsc/history?line=1&by=day&count=14").json()
        assert d["ok"] and len(d["rows"]) == 14 and d["day_start"] == 6 and d["rows"][-1]["now"]
        assert sum(r["pieces"] for r in d["rows"][:-1]) > 0 and not any(r["partial"] for r in d["rows"])
        w = c.get("/api/tsc/history?line=2&by=week&count=8").json()
        assert len(w["rows"]) == 8 and w["rows"][0]["label"].startswith("Week ")
        csv = c.get("/api/tsc/history.csv?line=1&by=week&count=3")
        assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("week,start,parts,pieces,feet")
        assert len(csv.text.strip().splitlines()) == 4
        assert c.get("/api/tsc/history?line=1&by=month").status_code == 400
        assert c.get("/api/tsc/status").json()["history"]["db"]["lines"] == 2
        assert (tmp_path / "production.db").exists()

        c.delete("/api/tsc/config")
        assert c.get("/api/tsc/history?line=1").json() == {"ok": False, "configured": False,
                                                          "error": "no TSC connection is set up yet (Production > Connection)"}

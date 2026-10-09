"""TSC part schedule: the production summary, the demo source, the API, and (optionally) a real SQL Server."""

import os
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ghostmap.analysis.production import current_shift, feet, shift_start, state, stop_window, summary
from ghostmap.collectors.tsc import COLUMNS, TscConfig, parse_shifts
from ghostmap.sim.tsc import PATTERNS, SimTsc, demo_service_items, pattern_hits, world
from ghostmap.web.app import create_app

H = {"x-ghostmap": "1"}
UNSET = datetime(1900, 1, 1)
NOW = datetime(2026, 10, 8, 14, 30)


def row(**kw):
    r = {"PartID": str(uuid.uuid4()), "OrderNumber": "7", "Status": 2, "StatusDescription": "Queued",
         "RequestedQuantity": 10, "QuantityAdjust": 0, "ActualQuantity": 0, "QuantityRemaining": 10, "Length": 120,
         "ProfileName": "R-Panel", "StartTime": UNSET, "EndTime": UNSET, "IsScrap": False, "IsForcedRemake": False,
         "RemakeCode": 0, "ErrorCode": 0, "LineID": 1, "LineName": "Line 1"}
    r.update(kw)
    return r


def test_states_feet_and_shifts():
    # TSC's status codes: 0 import, 1 pending, 2 queued, 3 hold, 4 in progress, 5 complete, 6 error.
    assert [state(row(Status=n)) for n in range(7)] == ["pending", "pending", "queued", "held", "running", "done", "error"]
    assert state(row(Status=None, StatusDescription="On Hold")) == "held"
    assert state(row(Status=None, StartTime=NOW)) == "running"
    assert state(row(Status=None, StartTime=NOW, EndTime=NOW)) == "done"
    assert state(row(Status=2, StartTime=UNSET)) == "queued"  # 1900-01-01 means "not yet"
    assert feet(row(ActualQuantity=10)) == 100.0  # 10 x 120 in
    assert feet(row(ActualQuantity=2, Length=10), "ft") == 20.0
    assert round(feet(row(ActualQuantity=1, Length=3048), "mm"), 3) == 10.0
    shifts = parse_shifts("18:00, 06:00")
    assert shifts == [(6, 0), (18, 0)]
    assert shift_start(NOW, shifts) == datetime(2026, 10, 8, 6, 0)
    assert shift_start(datetime(2026, 10, 8, 3, 0), shifts) == datetime(2026, 10, 7, 18, 0)
    with pytest.raises(ValueError):
        parse_shifts("6am")


def shift_row(name, day, start, end, day_name=None):
    return {"Name": name, "DayNumber": day, "DayName": day_name, "StartTime": datetime(1900, 1, 1, *start),
            "EndTime": datetime(1900, 1, 1, *end)}


def test_current_shift_from_the_tsc_calendar():
    # NOW is a Thursday. dbo.Shift times are smalldatetime: only the time of day counts.
    rows = [shift_row("Days", 5, (6, 0), (16, 30), "Thursday"), shift_row("Nights", 4, (22, 0), (6, 0), "Wednesday")]
    assert current_shift(NOW, rows) == {"name": "Days", "start": datetime(2026, 10, 8, 6), "end": datetime(2026, 10, 8, 16, 30)}
    # Wednesday's night shift runs past midnight into Thursday morning.
    assert current_shift(datetime(2026, 10, 8, 2, 0), rows)["name"] == "Nights"
    assert current_shift(datetime(2026, 10, 8, 18, 0), rows) is None  # between shifts
    # Without day names, DayNumber is SQL Server's default (1 = Sunday, so 5 = Thursday).
    assert current_shift(NOW, [shift_row("A", 5, (6, 0), (16, 0))])["name"] == "A"
    assert current_shift(NOW, [shift_row("A", 4, (6, 0), (16, 0))]) is None


def test_stop_window_clips_to_the_shift():
    since = datetime(2026, 10, 8, 6)
    stops = [{"StopTime": datetime(2026, 10, 8, 5, 50), "StartTime": datetime(2026, 10, 8, 6, 10), "StopCode": 1, "Description": "Coil change"},
             {"StopTime": datetime(2026, 10, 8, 14, 20), "StartTime": UNSET, "StopCode": 2, "Description": "Material jam"},
             {"StopTime": datetime(2026, 10, 8, 4, 0), "StartTime": datetime(2026, 10, 8, 4, 5), "StopCode": 3}]  # before the shift
    w = stop_window(stops, since, NOW)
    assert [(x["reason"], x["seconds"], x["ongoing"]) for x in w] == [("Material jam", 600, True), ("Coil change", 600, False)]


def test_summary_counts_orders_and_pieces_this_shift():
    shift = {"name": "Days", "start": datetime(2026, 10, 8, 6), "end": datetime(2026, 10, 8, 16, 30), "source": "tsc"}
    done = [row(OrderNumber="5", Status=5, StartTime=NOW - timedelta(hours=1), EndTime=NOW - timedelta(minutes=30),
                ActualQuantity=10, QuantityRemaining=0),
            row(OrderNumber="5", Status=5, StartTime=NOW - timedelta(hours=2), EndTime=NOW - timedelta(minutes=90),
                ActualQuantity=10, QuantityRemaining=0, IsScrap=True, RemakeCode=2),
            row(OrderNumber="4", Status=5, StartTime=NOW - timedelta(hours=10), EndTime=NOW - timedelta(hours=9), ActualQuantity=4)]
    # The queue view, in run order (QueueIndex): running, queued, held, queued.
    opened = [row(OrderNumber="8", Status=4, QueueIndex=0, StartTime=NOW - timedelta(minutes=10), ActualQuantity=5,
                  QuantityRemaining=5, BundleMark="B1"),
              row(OrderNumber="8", QueueIndex=1, BundleMark="B1"), row(OrderNumber="9", Status=3, QueueIndex=2, BundleMark="B2"),
              row(OrderNumber="9", QueueIndex=3, BundleMark="B3")]
    orders = [{"OrderNumber": "8", "Parts": 6, "PartsDone": 4, "Pieces": 60, "PiecesDone": 45, "Bundles": 2, "LengthLeft": 1800}]
    s = summary(opened, done, NOW, shift, orders=orders)
    t = s["totals"]
    assert (t["orders"], t["parts"], t["pieces"], t["feet"], t["scrap_pieces"], t["remakes"]) == (2, 2, 20, 200.0, 10, 1)
    assert t["pieces_per_hour"] == round(20 / 8.5, 1)
    assert s["shift"]["name"] == "Days" and s["shift"]["source"] == "tsc"
    run = s["running"][0]
    assert run["progress"] == 0.5 and run["elapsed_s"] == 600 and run["eta_s"] == 600
    assert [(p["queue"], p["state"]) for p in s["queue"]] == [(1, "queued"), (2, "held"), (3, "queued")]
    assert [p["order"] for p in s["held"]] == ["9"]
    assert s["queue_total"] == {"orders": 2, "bundles": 3, "parts": 3, "pieces": 30, "feet": 300.0}
    o8, o9 = s["orders"]
    assert (o8["order"], o8["running"], o8["queued_parts"], o8["pieces_done"], o8["bundles"], o8["feet_left"]) == ("8", True, 1, 45, 2, 150.0)
    assert (o9["order"], o9["held"], o9.get("pieces")) == ("9", 1, None)  # no whole-order totals: not guessed
    assert s["downtime"] is None and s["stations"] == []
    assert sum(b["parts"] for b in s["hourly"]) == 3 and len(s["hourly"]) == 12
    assert s["hourly"][-1]["parts"] == 1  # finished at 14:00, in the 14:00 bar


def test_paired_stations_and_a_skipped_station():
    shift = {"name": "", "start": datetime(2026, 10, 8, 6)}
    run = row(Status=4, QueueIndex=0, StartTime=NOW - timedelta(minutes=5), ActualQuantity=4, Quantity=8)
    nxt = row(QueueIndex=1)
    stations = [{"StationID": 3, "Name": "Pan"}, {"StationID": 4, "Name": "Back Skin"}, {"StationID": 5, "Name": "Sandwich"}]
    links = [{"DependentStationID": 5, "IndependentStationID": 3}, {"DependentStationID": 5, "IndependentStationID": 4}]
    sp = [{"StationID": 3, "PartID": run["PartID"], "QuantityCompleted": 6, "IsInProgress": True, "IsCompleted": False},
          {"StationID": 4, "PartID": run["PartID"], "QuantityCompleted": 8, "IsInProgress": False, "IsCompleted": True},
          {"StationID": 5, "PartID": run["PartID"], "QuantityCompleted": 4, "IsInProgress": True, "IsCompleted": False},
          {"StationID": 3, "PartID": nxt["PartID"]}, {"StationID": 5, "PartID": nxt["PartID"]}]  # skips the back skin
    s = summary([run, nxt], [], NOW, shift, stations=stations, station_parts=sp, links=links)
    got = {x["name"]: (x["state"], x["qty"], x["of"], x["waits_for"]) for x in s["stations"]}
    assert got == {"Pan": ("running", 6, 8, []), "Back Skin": ("done", 8, 8, []), "Sandwich": ("running", 4, 8, ["Pan", "Back Skin"])}
    assert [x["id"] for x in s["running"][0]["stations"]] == [3, 4, 5]
    assert [x["id"] for x in s["queue"][0]["stations"]] == [3, 5]
    # Without dbo.Station / StationPart: stations 1 and 2 from the view's own columns ('IP', 'C', 'x'; empty = skipped).
    v = row(Status=4, StartTime=NOW, Quantity=10, Station1Status="C", Station1ActualQuantity=10, Station2Status="IP",
            Station2ActualQuantity=3)
    s = summary([v, row(QueueIndex=1, Station1Status="x")], [], NOW, shift)
    assert [(x["name"], x["state"], x["qty"]) for x in s["stations"]] == [("Station 1", "done", 10), ("Station 2", "running", 3)]
    assert [x["id"] for x in s["queue"][0]["stations"]] == [1]


def test_sim_follows_the_tsc_views():
    w = world(NOW)
    rows = w["rows"]
    src = SimTsc()
    for line in (1, 2):
        mine = [r for r in rows if r["LineID"] == line]
        assert sum(r["Status"] == 4 for r in mine) <= 1
        assert sum(r["Status"] == 3 for r in mine) == 1                       # one part on hold
        assert sum(r["Status"] == 1 for r in mine) == 3                       # imported, not queued yet
        assert sum(r["Status"] == 5 for r in mine) > 20
        assert all(r["EndTime"] <= NOW for r in mine)
        q = [r for r in mine if r["Status"] in (2, 3, 4)]
        assert sorted(r["QueueIndex"] for r in q) == list(range(len(q)))
    assert set(COLUMNS) <= set(rows[0])  # the sim has every column the SQL source selects
    assert [x["LineID"] for x in src.lines()] == [1, 2]
    # Repeat patterns: lead 2", every 24", end 2" on a 120" part: holes at 2, 26, 50, 74, 98 = 5.
    rep = {"IsRepeat": True, "LeadOffset": 2.0, "RepeatOffset": 24.0, "EndOffset": 2.0}
    assert pattern_hits(rep, 120) == 5 and pattern_hits(rep, 3) == 0 and pattern_hits({"IsRepeat": False}, 120) == 1


def test_sim_strokes_match_the_parts_made():
    w = world()
    since = w["start"]
    done = {r["PartID"]: r for r in w["rows"] if r["LineID"] == 1 and r["Status"] == 5}
    want: dict = {}
    for pp in w["part_patterns"]:
        r = done.get(pp["PartID"])
        for tool, sid in PATTERNS[pp["PatternName"]] if r else ():
            want[(sid, tool)] = want.get((sid, tool), 0) + r["ActualQuantity"] * pattern_hits(pp, r["Length"])
    for h in w["holes"]:
        if h["PartID"] in done:
            want[(h["StationID"], h["ToolType"])] = want.get((h["StationID"], h["ToolType"]), 0) + done[h["PartID"]]["ActualQuantity"]
    got = {(x["StationID"], x["ToolType"]): x["Strokes"] for x in SimTsc().strokes(1, since)}
    assert got == want and got[(1, "RD25")] > 0 and got[(2, "N90")] > 0


def test_tsc_api_with_the_demo_source(tmp_path):
    app = create_app(str(tmp_path), demo=True, demo_opcua=False, collect=False)
    with TestClient(app) as c:
        assert c.get("/api/tsc/lines").json()["lines"][0] == {"id": 1, "name": "Line 1"}
        p = c.get("/api/tsc/production?line=2").json()
        assert p["ok"] and p["source"] == "demo" and p["totals"]["pieces"] > 0 and len(p["queue"]) == 14
        assert p["shift"]["source"] == "tsc" and p["shift"]["name"] in ("1st", "2nd", "3rd") and p["downtime"] is not None
        assert [x["name"] for x in p["stations"]] == ["Pan", "Back Skin", "Sandwich"]
        assert p["stations"][2]["waits_for"] == ["Pan", "Back Skin"]
        assert all(o.get("bundles") for o in p["orders"])
        status = c.get("/api/tsc/status").json()
        assert status["queries"] >= 1 and status["features"]["stations"]["ok"] is True
        assert "GRANT SELECT ON dbo.StationPart" in status["features"]["stations"]["grant"]
        k = c.get("/api/maintenance/counters?line=1").json()["tsc"]
        assert k["ok"] and k["periods"]["all"]["feet"] > k["periods"]["30d"]["feet"] > k["periods"]["24h"]["feet"] > 0
        assert {s["tool"] for s in k["strokes"]} >= {"RD50", "RD25", "N90"} and k["stations"][0] == {"id": 1, "name": "Punch / Rollformer"}
        # Saving: the password is stored protected and never sent back; a blank one keeps it.
        cfg = {"server": "sqlhost\\TSC", "database": "TSC", "username": "ghostmap_ro", "password": "s3cret!"}
        r = c.post("/api/tsc/config", json=cfg, headers=H).json()
        assert r["has_password"] and "password" not in r and r["queue_view"] == "DataView.vHmiPartScheduleQueueOrInProgress"
        assert "s3cret" not in (tmp_path / "tsc.json").read_text()
        c.post("/api/tsc/config", json={**cfg, "password": "", "shifts": "07:00"}, headers=H)
        assert app.state.tsc.cfg.password == "s3cret!" and app.state.tsc.cfg.shifts == "07:00"
        assert c.post("/api/tsc/config", json={**cfg, "view": "x; DROP TABLE y"}, headers=H).status_code == 400
        assert c.post("/api/tsc/config", json={**cfg, "queue_view": "dbo.x WHERE 1=1"}, headers=H).status_code == 400
        assert c.post("/api/tsc/config", json={**cfg, "shifts": "noon"}, headers=H).status_code == 400
        assert "s3cret" not in c.get("/api/export").text and "s3cret" not in c.get("/api/debug/bundle").content.decode("latin-1")
        # A failed connection is an answer, not a crash.
        bad = c.post("/api/tsc/test", json={**cfg, "server": "127.0.0.1,9", "password": "x"}, headers=H).json()
        assert bad["ok"] is False and bad["error"]
    app = create_app(str(tmp_path), collect=False)  # restart: the saved settings come back
    with TestClient(app):
        assert app.state.tsc.cfg.password == "s3cret!" and app.state.tsc.cfg.server == "sqlhost\\TSC"


def test_maintenance_items(tmp_path):
    app = create_app(str(tmp_path), demo=True, demo_opcua=False, collect=False)
    with TestClient(app, headers=H) as c:
        items = c.get("/api/maintenance").json()["items"]
        assert len(items) == len(demo_service_items()) and all(it["value"] is not None for it in items)
        assert {it["state"] for it in items} == {"ok", "soon", "overdue"}
        punch = next(it for it in items if it["kind"] == "strokes" and it["tool"] == "RD25")
        assert punch["state"] == "overdue" and punch["value"] >= 1_540_000 and punch["unit"] == "strokes"
        # Add, mark done (logged with the reading, count starts again), edit, delete.
        it = c.post("/api/maintenance/items", json={"name": "Notcher blades", "kind": "strokes", "line": 1, "station": 2,
                                                    "interval": 1000, "offset": 950}).json()
        got = next(x for x in c.get("/api/maintenance").json()["items"] if x["id"] == it["id"])
        assert got["state"] == "soon" and got["value"] >= 950
        done = c.post(f"/api/maintenance/items/{it['id']}/service", json={"note": "New blades"}).json()
        assert done["log"][0]["note"] == "New blades" and done["log"][0]["reading"] >= 950 and done["offset"] == 0
        got = next(x for x in c.get("/api/maintenance").json()["items"] if x["id"] == it["id"])
        assert got["value"] == 0 and got["state"] == "ok"
        assert c.post(f"/api/maintenance/items/{it['id']}", json={"interval": 2000}).json()["interval"] == 2000
        assert c.post("/api/maintenance/items", json={"name": "x", "kind": "strokes", "line": 1, "interval": 5}).status_code == 400
        assert c.post("/api/maintenance/items", json={"name": "x", "kind": "run_hours", "dash": "nope", "interval": 5}).status_code == 404
        assert c.post("/api/maintenance/items", json={"name": "x", "kind": "magic", "interval": 5}).status_code == 400
        assert c.delete(f"/api/maintenance/items/{it['id']}").json()["ok"]
        assert c.delete(f"/api/maintenance/items/{it['id']}").status_code == 404
        assert "maint.service" in (tmp_path / "audit.log").read_text()


def test_maintenance_run_hours_from_the_collector(tmp_path):
    from ghostmap.web.maintenance import due_state

    assert [due_state(v, 100, 90) for v in (10, 90, 100)] == ["ok", "soon", "overdue"]
    tags = [{"path": "Faults/E_Stop", "node_id": "ns=2;s=[P]F.E_Stop", "type": "Boolean", "value": "false"}]
    app = create_app(str(tmp_path), collect=False)
    with TestClient(app, headers=H) as c:
        d = c.post("/api/dashboards", json={"name": "Leveler", "endpoint": "demo", "tags": tags}).json()
        h = app.state.history
        h._st.setdefault(d["id"], None)
        h._state(d["id"])["acc"] = {"run_s": 7200.0, "online_s": 9000.0}  # as if the collector had counted 2 h
        it = c.post("/api/maintenance/items", json={"name": "Hydraulic filter", "kind": "run_hours", "dash": d["id"],
                                                    "interval": 500}).json()
        assert it["baseline"] == 2.0  # counts from now, not from when Ghost Map started counting
        h._state(d["id"])["acc"]["run_s"] += 1800
        got = c.get("/api/maintenance").json()["items"][0]
        assert got["value"] == 0.5 and got["unit"] == "h"
        m = c.get("/api/maintenance/counters").json()
        assert m["tsc"] is None and m["machines"][0]["run_h"] == 2.5 and m["machines"][0]["online_h"] == 2.5


# ---------------------------------------------------------------------------------------------------- real SQL
# Set GHOSTMAP_TEST_SQL="host,port;admin user;admin password" to run this against a scratch SQL Server (for
# example the mssql/server docker image). It builds a made-up TSC database from the simulated line (the part
# schedule views, stations, shifts, downtime and the pattern tables), a login that can only SELECT from the
# DataView schema, and reads it the way Ghost Map does; then grants the optional tables and reads them too.
SQL = os.environ.get("GHOSTMAP_TEST_SQL")
TYPES = {"PartID": "uniqueidentifier", "Thickness": "float", "StripWidth": "float", "WebWidth": "float",
         "Length": "float", "EntryDate": "datetime", "StartTime": "datetime", "EndTime": "datetime"}
EXTRA = [  # the optional tables, with TSC's column types
    "CREATE TABLE dbo.Station (StationID tinyint, LineID tinyint, Name nvarchar(50), Description nvarchar(100))",
    "CREATE TABLE dbo.StationPart (StationID tinyint, PartID uniqueidentifier, QuantityCompleted int, IsInProgress bit, IsCompleted bit)",
    "CREATE TABLE dbo.StationDependencyType (StationDependencyTypeID tinyint, Name nvarchar(50))",
    "CREATE TABLE dbo.StationDependency (StationDependencyID uniqueidentifier DEFAULT NEWID(), DependentStationID tinyint, "
    "IndependentStationID tinyint, StationDependencyTypeID tinyint)",
    "CREATE TABLE dbo.DayOfWeek (DayNumber tinyint, Name nvarchar(20))",
    "CREATE TABLE dbo.Shift (ShiftID smallint, Name nvarchar(50), DayNumber tinyint, ShiftNumber smallint, StartTime smalldatetime, "
    "EndTime smalldatetime)",
    "CREATE TABLE dbo.DownTimeCode (StopCode smallint, Description nvarchar(100))",
    "CREATE TABLE dbo.DownTime (LineID tinyint, StopTime datetime, StartTime datetime, StopCode smallint)",
    "CREATE VIEW dbo.vGetDownTimeData AS SELECT TOP (100) PERCENT dbo.DownTime.LineID, dbo.DownTime.StopTime, dbo.DownTime.StartTime, "
    "dbo.DownTime.StopCode, dbo.DownTimeCode.Description FROM dbo.DownTime INNER JOIN dbo.DownTimeCode ON "
    "dbo.DownTime.StopCode = dbo.DownTimeCode.StopCode ORDER BY dbo.DownTime.StopTime DESC",
    "CREATE TABLE dbo.PartPattern (PartPatternID uniqueidentifier DEFAULT NEWID(), PartID uniqueidentifier, PatternName nvarchar(50), "
    "ReferenceID tinyint, LeadOffset real, IsRepeat bit, RepeatOffset real, EndOffset real)",
    "CREATE TABLE dbo.PatternHole (PatternHoleID uniqueidentifier DEFAULT NEWID(), PatternName nvarchar(50), ToolType nvarchar(10), "
    "XOffset real, YOffset real, ZOffset real, StationID tinyint)",
    "CREATE TABLE dbo.Hole (HoleID uniqueidentifier DEFAULT NEWID(), PartID uniqueidentifier, ToolType nvarchar(10), StationID tinyint)",
    "CREATE TABLE dbo.PartNotch (PartNotchID uniqueidentifier DEFAULT NEWID(), PartID uniqueidentifier, ToolType nvarchar(10), StationID tinyint)",
    "CREATE TABLE Machine.ToolType (HoleType nvarchar(10), Description nvarchar(100))",
]
GRANTS = ["dbo.Station", "dbo.StationPart", "dbo.StationDependency", "dbo.StationDependencyType", "dbo.Shift", "dbo.DayOfWeek",
          "dbo.vGetDownTimeData", "dbo.PartPattern", "dbo.PatternHole", "dbo.Hole", "dbo.PartNotch", "Machine.ToolType"]


def _seed(cur, w):
    """The simulated line as TSC tables."""
    from ghostmap.sim import tsc as sim

    def ins(table, cols, rows):
        if rows:
            cur.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                            [tuple(r[c] if isinstance(r, dict) else r[i] for i, c in enumerate(cols)) for r in rows])

    ins("dbo.Station", ["StationID", "LineID", "Name", "Description"], sim.STATIONS)
    ins("dbo.StationPart", ["StationID", "PartID", "QuantityCompleted", "IsInProgress", "IsCompleted"], w["station_parts"])
    ins("dbo.StationDependencyType", ["StationDependencyTypeID", "Name"], list(sim.DEPENDENCY_TYPES.items()))
    ins("dbo.StationDependency", ["DependentStationID", "IndependentStationID", "StationDependencyTypeID"], sim.DEPENDENCIES)
    ins("dbo.DayOfWeek", ["DayNumber", "Name"], list(sim.DAY_NAMES.items()))
    ins("dbo.Shift", ["ShiftID", "Name", "DayNumber", "ShiftNumber", "StartTime", "EndTime"], SimTsc().shifts())
    ins("dbo.DownTimeCode", ["StopCode", "Description"], list(sim.STOP_CODES.items()))
    ins("dbo.DownTime", ["LineID", "StopTime", "StartTime", "StopCode"], w["downtime"])
    ins("dbo.PartPattern", ["PartID", "PatternName", "LeadOffset", "IsRepeat", "RepeatOffset", "EndOffset"], w["part_patterns"])
    ins("dbo.PatternHole", ["PatternName", "ToolType", "StationID"],
        [(name, tool, sid) for name, holes in sim.PATTERNS.items() for tool, sid in holes])
    ins("dbo.Hole", ["PartID", "ToolType", "StationID"], w["holes"])
    ins("Machine.ToolType", ["HoleType", "Description"], list(sim.TOOL_TYPES.items()))


@pytest.mark.skipif(not SQL, reason="set GHOSTMAP_TEST_SQL to test against a real SQL Server")
def test_real_sql_server_read_only_login(tmp_path):
    import asyncio

    import pytds

    from ghostmap.collectors.tsc import SqlTsc
    from ghostmap.web.tsc import TscService

    server, admin, admin_pw = SQL.split(";")
    host, port = server.split(",")
    w = world(NOW)
    rows = w["rows"]
    cols = list(dict.fromkeys(k for r in rows for k in r))
    sample = {k: next(r[k] for r in rows if r.get(k) is not None) for k in cols}

    def coltype(k, v):
        if k in TYPES:
            return TYPES[k]
        return "bit" if isinstance(v, bool) else "int" if isinstance(v, int) else "nvarchar(100)"

    db, login, pw = "GhostMapTestTSC", "ghostmap_test_ro", "Ro!" + uuid.uuid4().hex[:12]

    def admin_sql(*stmts):
        with pytds.connect(dsn=host, port=int(port), user=admin, password=admin_pw, autocommit=True) as conn:
            cur = conn.cursor()
            cur.execute(f"USE {db}")
            for x in stmts:
                cur.execute(x)

    with pytds.connect(dsn=host, port=int(port), user=admin, password=admin_pw, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(f"IF DB_ID('{db}') IS NOT NULL BEGIN ALTER DATABASE {db} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                    f"DROP DATABASE {db}; END")
        cur.execute(f"IF SUSER_ID('{login}') IS NOT NULL DROP LOGIN {login}")
        cur.execute(f"CREATE DATABASE {db}")
        cur.execute(f"USE {db}")
        cur.execute("EXEC('CREATE SCHEMA DataView')")
        cur.execute("EXEC('CREATE SCHEMA Machine')")
        cur.execute("CREATE TABLE dbo.PartSchedule (" + ", ".join(f"[{k}] {coltype(k, sample[k])} NULL" for k in cols) + ")")
        cur.executemany("INSERT INTO dbo.PartSchedule VALUES (" + ", ".join(["%s"] * len(cols)) + ")",
                        [tuple(r.get(k) for k in cols) for r in rows])
        cur.execute("EXEC('CREATE VIEW DataView.vPartScheduleCommon AS SELECT * FROM dbo.PartSchedule')")
        cur.execute("EXEC('CREATE VIEW DataView.vHmiPartScheduleQueueOrInProgress AS SELECT * FROM dbo.PartSchedule WHERE Status IN (2, 3, 4)')")
        cur.execute("EXEC('CREATE VIEW DataView.vPartScheduleCompleted AS SELECT * FROM dbo.PartSchedule WHERE Status = 5')")
        for x in EXTRA:
            cur.execute(f"EXEC('{x}')" if x.startswith("CREATE VIEW") else x)
        _seed(cur, w)
        cur.execute(f"CREATE LOGIN {login} WITH PASSWORD = '{pw}', CHECK_POLICY = OFF")
        cur.execute(f"CREATE USER {login} FOR LOGIN {login}")
        cur.execute(f"GRANT SELECT ON SCHEMA::DataView TO {login}")
    try:
        cfg = TscConfig(server=server, database=db, username=login, password=pw)
        src = SqlTsc(cfg)
        assert [r["LineID"] for r in src.lines()] == [1, 2]
        opened = src.open_parts(1)
        assert [r["QueueIndex"] for r in opened] == list(range(len(opened)))  # run order, pending parts left out
        assert {r["Status"] for r in opened} <= {2, 3, 4} and any(r["Status"] == 3 for r in opened)
        since = NOW - timedelta(hours=12)
        done = src.done_parts(1, since)
        want = [r for r in rows if r["LineID"] == 1 and r["Status"] == 5 and r["EndTime"] >= since]
        assert len(done) == len(want)
        shift = {"name": "", "start": datetime(2026, 10, 8, 6)}
        s = summary(opened, done, NOW, shift)
        sim = summary(sorted([r for r in rows if r["LineID"] == 1 and r["Status"] in (2, 3, 4)], key=lambda r: r["QueueIndex"]),
                      want, NOW, shift)
        assert s["totals"] == sim["totals"] and s["queue_total"] == sim["queue_total"]
        orders = src.order_totals(1, [r["OrderNumber"] for r in opened])
        o = {x["OrderNumber"]: x for x in orders}
        first = opened[0]["OrderNumber"]
        mine = [r for r in rows if r["LineID"] == 1 and r["OrderNumber"] == first]
        assert (o[first]["Parts"], o[first]["Pieces"], o[first]["Bundles"]) == (
            len(mine), sum(r["Quantity"] for r in mine), len({r["BundleID"] for r in mine}))
        t = src.totals(1, since)
        assert (t["Parts"], t["Pieces"]) == (len(want), sum(r["ActualQuantity"] for r in want))

        # Only DataView granted: the optional reads say so and the page still works.
        svc = TscService(tmp_path)
        svc.cfg = cfg
        p = asyncio.run(svc.production(2))
        assert p["queue"] and p["stations"] == [] and p["downtime"] is None and p["shift"]["source"] == "settings"
        feats = svc.status()["features"]
        assert feats["stations"]["ok"] is False and "permission was denied" in feats["stations"]["error"]
        assert feats["stations"]["grant"].startswith("GRANT SELECT ON dbo.Station TO ghostmap_test_ro;")
        assert asyncio.run(svc.counters(1))["strokes_ok"] is False

        # Grant the lookups (as the Connection page asks) and read them.
        admin_sql(*(f"GRANT SELECT ON {t} TO {login}" for t in GRANTS))
        assert [x["Name"] for x in src.stations() if x["LineID"] == 2] == ["Pan", "Back Skin", "Sandwich"]
        assert {(x["DependentStationID"], x["IndependentStationID"], x["TypeName"]) for x in src.station_links()} == {
            (5, 3, "Requires"), (5, 4, "Requires")}
        assert current_shift(NOW, src.shifts())["name"] == "2nd"  # 14:00 to 22:00
        assert len(src.station_parts([str(r["PartID"]) for r in opened])) == len(
            [sp for sp in w["station_parts"] if sp["PartID"] in {str(r["PartID"]) for r in opened}])
        stops = src.downtime(1, datetime(2026, 10, 8, 6))
        assert stops and all(x["Description"] for x in stops)
        # Strokes from the pattern tables match the simulator's own count, repeats along the part included.
        sim_strokes: dict = {}
        done_ids = {r["PartID"]: r for r in want}
        for pp in w["part_patterns"]:
            r = done_ids.get(pp["PartID"])
            for tool, sid in PATTERNS[pp["PatternName"]] if r else ():
                sim_strokes[(sid, tool)] = sim_strokes.get((sid, tool), 0) + r["ActualQuantity"] * pattern_hits(pp, r["Length"])
        for h in w["holes"]:
            if h["PartID"] in done_ids:
                k = (h["StationID"], h["ToolType"])
                sim_strokes[k] = sim_strokes.get(k, 0) + done_ids[h["PartID"]]["ActualQuantity"]
        assert {(x["StationID"], x["ToolType"]): x["Strokes"] for x in src.strokes(1, since)} == sim_strokes
        svc = TscService(tmp_path)
        svc.cfg = cfg
        asyncio.run(svc.production(2))
        assert all(f["ok"] for f in svc.status()["features"].values() if f["ok"] is not None)
        k = asyncio.run(svc.counters(1))
        assert k["strokes_ok"] and k["tools"] and k["stations"] == [{"id": 1, "name": "Punch / Rollformer"}, {"id": 2, "name": "Notcher"}]

        enc = SqlTsc(TscConfig(server=server, database=db, username=login, password=pw, encrypt=True))
        assert len(enc.lines()) == 2  # encrypted, trusting the server's self-signed certificate
        # The login really is read-only: it can't change the view or see the table behind it.
        with src._connect() as conn:
            cur = conn.cursor()
            with pytest.raises(pytds.Error, match="permission was denied"):
                cur.execute("UPDATE DataView.vPartScheduleCommon SET Status = 0")
            with pytest.raises(pytds.Error, match="permission was denied"):
                cur.execute("SELECT TOP 1 * FROM dbo.PartSchedule")
            with pytest.raises(pytds.Error, match="permission was denied"):
                cur.execute("DELETE FROM dbo.StationPart")
    finally:
        with pytds.connect(dsn=host, port=int(port), user=admin, password=admin_pw, autocommit=True) as conn:
            cur = conn.cursor()
            cur.execute(f"ALTER DATABASE {db} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            cur.execute(f"DROP DATABASE {db}")
            cur.execute(f"DROP LOGIN {login}")


def test_export_and_import_round_trip(tmp_path):
    rows = [{"path": f"Faults/{n}", "node_id": f"ns=2;s=[P]Faults.{n}", "type": "Boolean", "value": "false"}
            for n in ("E_Stop", "Drive_Flt", "Low_Air")]
    a = create_app(str(tmp_path / "a"), collect=False)
    with TestClient(a, headers=H) as c:
        d = c.post("/api/dashboards", json={"name": "Leveler 1", "endpoint": "opc.tcp://10.0.0.5:4990", "tags": rows}).json()
        area = d["layout"]["areas"][0]["id"]
        c.post(f"/api/dashboards/{d['id']}/overrides", json={"invert": {area: True}, "hidden": [rows[2]["node_id"]]})
        c.post("/api/tsc/config", json={"server": "sql1", "database": "TSC", "username": "ro", "password": "pw-123"})
        r = c.get("/api/export")
        assert "attachment" in r.headers["content-disposition"]
        data = r.json()
        assert "pw-123" not in r.text and data["tsc"]["server"] == "sql1" and len(data["dashboards"][0]["tags"]) == 3
        one = c.get(f"/api/export?dash={d['id']}&settings=false").json()
        assert "tsc" not in one and len(one["dashboards"]) == 1

    b = create_app(str(tmp_path / "b"), collect=False)
    with TestClient(b, headers=H) as c:
        r = c.post("/api/import", json={"data": data}).json()
        assert r["added"] == [{"id": d["id"], "name": "Leveler 1"}] and r["tsc"] and not r["problems"]
        got = c.get(f"/api/dashboards/{d['id']}").json()
        assert got["overrides"] == {"invert": {area: True}, "hidden": [rows[2]["node_id"]]}
        assert got["layout"]["areas"] == d["layout"]["areas"]
        assert c.get(f"/api/dashboards/{d['id']}/tags?q=air").json()[0]["node_id"] == rows[2]["node_id"]
        assert b.state.tsc.cfg.server == "sql1" and b.state.tsc.cfg.password == ""  # passwords never travel
        # Importing again adds a copy under a new id, pointed at another gateway; nothing is overwritten.
        r = c.post("/api/import", json={"data": data, "endpoint": "demo", "settings": False}).json()
        assert r["added"][0]["id"] != d["id"] and c.get(f"/api/dashboards/{r['added'][0]['id']}").json()["endpoint"] == "demo"
        # A tag that isn't in the file's own tag list is refused, like an edit in the UI.
        bad = {**data, "dashboards": [{**data["dashboards"][0], "tags": data["dashboards"][0]["tags"][:1]}]}
        r = c.post("/api/import", json={"data": bad, "settings": False}).json()
        assert not r["added"] and "not in this dashboard's tag export" in r["problems"][0]
        assert c.post("/api/import", json={"data": {"kind": "other"}}).status_code == 400
        assert len(c.get("/api/dashboards").json()) == 2

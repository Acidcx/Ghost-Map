"""TSC part schedule: the production summary, the demo source, the API, and (optionally) a real SQL Server."""

import os
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ghostmap.analysis.production import feet, shift_start, state, summary
from ghostmap.collectors.tsc import COLUMNS, TscConfig, parse_shifts
from ghostmap.sim.tsc import SimTsc, schedule
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
    assert state(row(StartTime=None, EndTime=None)) == "queued"
    assert state(row(StatusDescription="On Hold", StartTime=None, EndTime=None)) == "held"
    assert state(row(StartTime=NOW, EndTime=None)) == "running"
    assert state(row(StartTime=NOW, EndTime=NOW)) == "done"
    assert feet(row(ActualQuantity=10)) == 100.0  # 10 x 120 in
    assert feet(row(ActualQuantity=2, Length=10), "ft") == 20.0
    assert round(feet(row(ActualQuantity=1, Length=3048), "mm"), 3) == 10.0
    shifts = parse_shifts("18:00, 06:00")
    assert shifts == [(6, 0), (18, 0)]
    assert shift_start(NOW, shifts) == datetime(2026, 10, 8, 6, 0)
    assert shift_start(datetime(2026, 10, 8, 3, 0), shifts) == datetime(2026, 10, 7, 18, 0)
    with pytest.raises(ValueError):
        parse_shifts("6am")


def test_summary_counts_this_shift_and_reads_1900_as_unset():
    done = [row(StartTime=NOW - timedelta(hours=1), EndTime=NOW - timedelta(minutes=30), ActualQuantity=10,
                QuantityRemaining=0),
            row(StartTime=NOW - timedelta(hours=2), EndTime=NOW - timedelta(minutes=90), ActualQuantity=10,
                QuantityRemaining=0, IsScrap=True, RemakeCode=2),
            row(StartTime=NOW - timedelta(hours=10), EndTime=NOW - timedelta(hours=9), ActualQuantity=4)]  # last shift
    opened = [row(OrderNumber="8", StartTime=NOW - timedelta(minutes=10), ActualQuantity=5, QuantityRemaining=5),
              row(OrderNumber="8"), row(OrderNumber="9", StatusDescription="Hold - material")]
    s = summary(opened, done, NOW, parse_shifts("06:00,18:00"))
    t = s["totals"]
    assert (t["parts"], t["pieces"], t["feet"], t["scrap"], t["remakes"]) == (2, 20, 200.0, 1, 1)
    assert t["pieces_per_hour"] == round(20 / 8.5, 1)
    run = s["running"][0]
    assert run["progress"] == 0.5 and run["elapsed_s"] == 600 and run["eta_s"] == 600
    assert [p["state"] for p in s["queue"]] == ["queued"] and s["queue"][0]["start"] is None
    assert [p["status"] for p in s["held"]] == ["Hold - material"]
    assert [(o["order"], o["open_parts"], o["running"], o["held"]) for o in s["orders"]] == [("8", 2, True, 0), ("9", 1, False, 1)]
    assert sum(b["parts"] for b in s["hourly"]) == 3 and len(s["hourly"]) == 12
    assert s["hourly"][-1]["parts"] == 1  # finished at 14:00, in the 14:00 bar


def test_sim_schedule_moves_with_the_clock():
    rows = schedule(NOW)
    for line in (1, 2):
        mine = [r for r in rows if r["LineID"] == line]
        assert sum(state(r) == "running" for r in mine) <= 1
        assert sum(r["StatusDescription"] == "On Hold" for r in mine) == 1
        assert sum(state(r) == "done" for r in mine) > 20
        assert all(r["EndTime"] <= NOW for r in mine)
    assert set(COLUMNS) <= set(rows[0])  # the sim has every column the SQL source selects
    src = SimTsc()
    assert [x["LineID"] for x in src.lines()] == [1, 2]


def test_tsc_api_with_the_demo_source(tmp_path):
    app = create_app(str(tmp_path), demo=True, demo_opcua=False, collect=False)
    with TestClient(app) as c:
        assert c.get("/api/tsc/lines").json()["lines"][0] == {"id": 1, "name": "Line 1"}
        p = c.get("/api/tsc/production?line=2").json()
        assert p["ok"] and p["source"] == "demo" and p["totals"]["parts"] > 0 and len(p["queue"]) == 10
        assert c.get("/api/tsc/status").json()["queries"] >= 1
        # Saving: the password is stored protected and never sent back; a blank one keeps it.
        cfg = {"server": "sqlhost\\TSC", "database": "TSC", "username": "ghostmap_ro", "password": "s3cret!"}
        r = c.post("/api/tsc/config", json=cfg, headers=H).json()
        assert r["has_password"] and "password" not in r
        assert "s3cret" not in (tmp_path / "tsc.json").read_text()
        c.post("/api/tsc/config", json={**cfg, "password": "", "shifts": "07:00"}, headers=H)
        assert app.state.tsc.cfg.password == "s3cret!" and app.state.tsc.cfg.shifts == "07:00"
        assert c.post("/api/tsc/config", json={**cfg, "view": "x; DROP TABLE y"}, headers=H).status_code == 400
        assert c.post("/api/tsc/config", json={**cfg, "shifts": "noon"}, headers=H).status_code == 400
        assert "s3cret" not in c.get("/api/export").text and "s3cret" not in c.get("/api/debug/bundle").content.decode("latin-1")
        # A failed connection is an answer, not a crash.
        bad = c.post("/api/tsc/test", json={**cfg, "server": "127.0.0.1,9", "password": "x"}, headers=H).json()
        assert bad["ok"] is False and bad["error"]
    app = create_app(str(tmp_path), collect=False)  # restart: the saved settings come back
    with TestClient(app):
        assert app.state.tsc.cfg.password == "s3cret!" and app.state.tsc.cfg.server == "sqlhost\\TSC"


# ---------------------------------------------------------------------------------------------------- real SQL
# Set GHOSTMAP_TEST_SQL="host,port;admin user;admin password" to run this against a scratch SQL Server (for
# example the mssql/server docker image). It builds a made-up TSC database from the simulated schedule, a login
# that can only SELECT from the DataView schema, and reads it the way Ghost Map does.
SQL = os.environ.get("GHOSTMAP_TEST_SQL")
TYPES = {"PartID": "uniqueidentifier", "Thickness": "float", "StripWidth": "float", "WebWidth": "float",
         "Length": "float", "EntryDate": "datetime", "StartTime": "datetime", "EndTime": "datetime"}


@pytest.mark.skipif(not SQL, reason="set GHOSTMAP_TEST_SQL to test against a real SQL Server")
def test_real_sql_server_read_only_login():
    import pytds

    from ghostmap.collectors.tsc import SqlTsc

    server, admin, admin_pw = SQL.split(";")
    host, port = server.split(",")
    rows = schedule(NOW)
    cols = list(rows[0])

    def coltype(k, v):
        if k in TYPES:
            return TYPES[k]
        return "bit" if isinstance(v, bool) else "int" if isinstance(v, int) else "nvarchar(100)"

    db, login, pw = "GhostMapTestTSC", "ghostmap_test_ro", "Ro!" + uuid.uuid4().hex[:12]
    with pytds.connect(dsn=host, port=int(port), user=admin, password=admin_pw, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(f"IF DB_ID('{db}') IS NOT NULL BEGIN ALTER DATABASE {db} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                    f"DROP DATABASE {db}; END")
        cur.execute(f"IF SUSER_ID('{login}') IS NOT NULL DROP LOGIN {login}")
        cur.execute(f"CREATE DATABASE {db}")
        cur.execute(f"USE {db}")
        cur.execute("EXEC('CREATE SCHEMA DataView')")
        cur.execute("CREATE TABLE dbo.PartSchedule (" + ", ".join(f"[{k}] {coltype(k, rows[0][k])} NULL" for k in cols) + ")")
        cur.executemany("INSERT INTO dbo.PartSchedule VALUES (" + ", ".join(["%s"] * len(cols)) + ")",
                        [tuple(r[k] for k in cols) for r in rows])
        cur.execute("EXEC('CREATE VIEW DataView.vPartScheduleCommon AS SELECT * FROM dbo.PartSchedule')")
        cur.execute(f"CREATE LOGIN {login} WITH PASSWORD = '{pw}', CHECK_POLICY = OFF")
        cur.execute(f"CREATE USER {login} FOR LOGIN {login}")
        cur.execute(f"GRANT SELECT ON SCHEMA::DataView TO {login}")
    try:
        cfg = TscConfig(server=server, database=db, username=login, password=pw)
        src = SqlTsc(cfg)
        assert [r["LineID"] for r in src.lines()] == [1, 2]
        opened = src.open_parts(1)
        assert opened and all(state(r) != "done" for r in opened)
        since = NOW - timedelta(hours=12)
        done = src.done_parts(1, since)
        want = [r for r in rows if r["LineID"] == 1 and r["EndTime"] >= since]
        assert len(done) == len(want)
        s = summary(opened, done, NOW, parse_shifts("06:00,18:00"))
        sim = summary([r for r in rows if r["LineID"] == 1 and r["EndTime"] <= UNSET], want, NOW, parse_shifts("06:00,18:00"))
        assert s["totals"] == sim["totals"] and len(s["queue"]) == len(sim["queue"])
        enc = SqlTsc(TscConfig(server=server, database=db, username=login, password=pw, encrypt=True))
        assert len(enc.lines()) == 2  # encrypted, trusting the server's self-signed certificate
        # The login really is read-only: it can't change the view or see the table behind it.
        with src._connect() as conn:
            cur = conn.cursor()
            with pytest.raises(pytds.Error, match="permission was denied"):
                cur.execute("UPDATE DataView.vPartScheduleCommon SET Status = 0")
            with pytest.raises(pytds.Error, match="permission was denied"):
                cur.execute("SELECT TOP 1 * FROM dbo.PartSchedule")
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

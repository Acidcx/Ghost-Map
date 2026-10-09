import asyncio

from fastapi.testclient import TestClient

from ghostmap.netmon import NetMonitor, NetmonConfig
from ghostmap.sim.machine import demo_switch_client
from ghostmap.web.app import create_app

H = {"X-Ghostmap": "1"}


def _run_polls(mon, ip, client, t, steps, step_s=30.0):
    st = {"open": {}, "prev": None, "layout": None, "layout_at": 0.0, "last_ok": None, "error": None, "polls": 0,
          "latest": {}, "cpu": None}
    mon._st[ip] = st

    async def go():
        for _ in range(steps):
            await mon.poll_once(ip, client, st)
            t[0] += step_s
    asyncio.run(go())
    return st


def test_monitor_records_rates_and_storm_events(tmp_path):
    t = [3600.0]  # demo storm: 60 s of every 1800 s, starting at t = 0, 1800, 3600, 5400 ...
    client = demo_switch_client("today", clock=lambda: t[0])
    mon = NetMonitor(tmp_path, clock=lambda: t[0], mono=lambda: t[0])
    mon.cfg = NetmonConfig(switches=["demo"], interval_s=30)
    st = _run_polls(mon, "demo", client, t, steps=62)  # 3600 .. 5430: storms at 3600-3660 and from 5400

    latest = st["latest"]
    assert latest["Fa1/7"]["in_util"] < 40  # the camera swings 85 -> 35 -> 85 Mbps over an hour
    assert latest["Fa1/2"]["in_err"] == 2.0
    rows = mon.store.series("demo", "Fa1/6", 3500, 5500, points=2000)
    assert max(r["in_bcast"] for r in rows) > 1000 and min(r["in_bcast"] for r in rows) < 1

    events = mon.store.events(0)
    storms = sorted((e for e in events if e["code"] == "port.broadcast_storm"), key=lambda e: e["start"])
    assert [e["port"] for e in storms] == ["Fa1/6", "Fa1/6"]  # two storms, the first one over
    assert storms[0]["end"] is not None and storms[1]["end"] is None
    assert storms[0]["peak"] >= 2000 and storms[0]["unit"] == "pkt/s"
    busy = [e for e in events if e["code"] == "port.utilization"]
    assert [e["port"] for e in busy] == ["Fa1/7"] and busy[0]["end"] is not None and busy[0]["peak"] > 80
    assert any(e["code"] == "port.errors_rising" and e["port"] == "Fa1/2" for e in events)
    assert not any(e["port"] in ("Fa1/3", "Fa1/4") for e in events)  # quiet VFDs: nothing to report

    peaks = mon.store.peaks("demo", 0)
    assert peaks["Fa1/7"]["util"] > 70
    assert "Fa1/6" in mon.store.events_csv(0)


def test_restart_closes_open_events(tmp_path):
    t = [3600.0]
    client = demo_switch_client("today", clock=lambda: t[0])
    mon = NetMonitor(tmp_path, clock=lambda: t[0], mono=lambda: t[0])
    mon.cfg = NetmonConfig(switches=["demo"], interval_s=30)
    _run_polls(mon, "demo", client, t, steps=3)
    assert any(e["end"] is None for e in mon.store.events(0))
    mon.store.close()
    again = NetMonitor(tmp_path)
    assert all(e["end"] is not None for e in again.store.events(0))
    again.store.close()


def test_config_saved_with_secrets_sealed(tmp_path):
    mon = NetMonitor(tmp_path)
    try:
        mon.merged({"switches": [" 10.0.0.2 ", "10.0.0.2"], "community": ""})
        raise AssertionError("community should be required")
    except ValueError:
        pass
    cfg = mon.merged({"switches": ["10.0.0.2", "10.0.0.3"], "community": "s3cret", "interval_s": 60})
    mon.save(cfg)
    assert "s3cret" not in (tmp_path / "netmon.json").read_text()
    mon.store.close()
    again = NetMonitor(tmp_path)
    assert again.cfg.community == "s3cret" and again.cfg.switches == ["10.0.0.2", "10.0.0.3"]
    assert again.merged({"switches": ["10.0.0.2"], "community": ""}).community == "s3cret"  # blank keeps it
    assert "community" not in again.public() and again.public()["has_community"]
    again.store.close()


def test_netmon_api_with_demo_switch(tmp_path):
    app = create_app(str(tmp_path), demo=True, demo_opcua=False, collect=False)
    with TestClient(app, headers=H) as c:
        cfg = c.get("/api/netmon/config").json()
        assert cfg["switches"] == ["demo", "demo2"]
        r = c.post("/api/netmon/test", json={"switches": ["demo", "demo2"]}).json()
        assert r["ok"] and [(s["name"], s["ports"]) for s in r["switches"]] == [("CELL1-SW01", 11), ("CELL1-SW02", 5)]
        assert c.post("/api/netmon/config", json={"switches": ["10.1.1.1"]}).status_code == 400  # no community
        saved = c.post("/api/netmon/config", json={"switches": ["demo"], "interval_s": 15}).json()
        assert saved["interval_s"] == 15
        st = c.get("/api/netmon/status").json()
        assert st["configured"] and st["switches"][0]["switch"] == "demo"
        assert c.get("/api/netmon/now?switch=demo").json()["ports"] == []  # not polled (collect=False)
        assert c.get("/api/netmon/events").json() == {"events": []}
        assert c.get("/api/netmon/series?switch=demo&port=Fa1/6").json()["rates"] == []
        assert c.delete("/api/netmon/config").json() == {"ok": True}
        assert not c.get("/api/netmon/status").json()["configured"]


def test_daisy_chained_switches_and_storm_passing_through(tmp_path):
    """Demo: CELL1-SW02 hangs off CELL1-SW01 Gi1/3, its drives are daisy-chained on Fa1/1, and the HMI's
    broadcast storm on SW01 Fa1/6 crosses the link into SW02 Gi1/1."""
    mon = NetMonitor(tmp_path, demo=True)
    asyncio.run(mon.seed_demo(hours=1))
    assert [(ip, depth, via) for ip, depth, via in mon.chain_order()] == [("demo", 0, ""), ("demo2", 1, "CELL1-SW01 Gi1/3")]
    sw01, sw02 = mon._layouts["demo"], mon._layouts["demo2"]
    assert sw01.port_by_name("Gi1/3").link_to == "CELL1-SW02 Gi1/1"
    assert sw02.port_by_name("Gi1/1").link_to == "CELL1-SW01 Gi1/3"
    assert sw01.port_by_name("Gi1/2").link_to == "PLANT-CORE Gi1/0/24"
    daisy = sw02.port_by_name("Fa1/1")
    assert not daisy.is_uplink and len(daisy.macs) == 6  # six drives, not a link to another switch

    evs = mon.events(0)
    storms = {(e["switch"], e["port"]): e for e in evs if e["code"] == "port.broadcast_storm"}
    assert "passing_from" not in storms[("demo", "Fa1/6")]
    assert storms[("demo2", "Gi1/1")]["passing_from"] == ["CELL1-SW01 Fa1/6"]
    assert "6 devices" not in storms[("demo", "Fa1/6")]["message"]
    mon.store.close()

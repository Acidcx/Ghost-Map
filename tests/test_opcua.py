import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghostmap.collectors import opcua
from ghostmap.collectors.opcua import normalize_endpoint
from ghostmap.web.app import create_app

H = {"X-Ghostmap": "1"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def ua(tmp_path_factory):
    """Web app with the simulated FT Linx-style OPC UA server running."""
    d = tmp_path_factory.mktemp("ua")
    with TestClient(create_app(data_dir=str(d), demo_opcua=True, demo_opcua_port=free_port()), headers=H) as c:
        url = c.get("/api/info").json()["demo_opcua"]
        assert url
        sid = c.post("/api/opcua/connect", json={"url": url}).json()["sid"]
        yield c, url, sid, d


def run_export(c, sid, node_id):
    import time

    job = c.post("/api/opcua/export", json={"sid": sid, "node_id": node_id}).json()["job"]
    for _ in range(100):
        j = c.get(f"/api/opcua/export/{job}").json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    assert j["visited"] > 0 and j["tags"] == len(j["result"]["tags"])
    return j["result"]


def find(c, sid, node_id, name):
    kids = c.post("/api/opcua/browse", json={"sid": sid, "node_id": node_id}).json()
    return next(k for k in kids if k["name"] == name)


def test_normalize_endpoint():
    assert normalize_endpoint("192.168.1.10") == "opc.tcp://192.168.1.10:4990"
    assert normalize_endpoint("hmi01:4991") == "opc.tcp://hmi01:4991"
    assert normalize_endpoint("opc.tcp://10.0.0.5/Gateway") == "opc.tcp://10.0.0.5:4990/Gateway"
    with pytest.raises(ValueError):
        normalize_endpoint("http://10.0.0.5")
    with pytest.raises(ValueError):
        normalize_endpoint("  ")


def test_client_has_no_write_paths():
    src = Path(opcua.__file__).read_text()
    for forbidden in ("write_value", "set_value", ".write(", "call_method", "add_nodes", "delete_nodes"):
        assert forbidden not in src, forbidden


def test_endpoints_and_connect(ua):
    c, url, sid, _ = ua
    eps = c.post("/api/opcua/endpoints", json={"url": url}).json()
    assert eps[0]["security_policy"] == "None" and "Anonymous" in eps[0]["user_tokens"]


def test_browse_attributes_read(ua):
    c, url, sid, _ = ua
    gw = find(c, sid, None, "FactoryTalk Linx Gateway")
    press = find(c, sid, gw["node_id"], "PRESS_01")
    tag = find(c, sid, press["node_id"], "PressFireCount")
    assert tag["node_class"] == "Variable" and tag["node_id"] == "ns=2;s=[PRESS_01]PressFireCount"
    a = c.post("/api/opcua/attributes", json={"sid": sid, "node_id": tag["node_id"]}).json()
    assert a["DataTypeName"] == "Int32" and a["AccessLevel"] == "Read"
    assert a["Value"]["status"] == "Good" and a["Value"]["value"] >= 1_204_331
    r = c.post("/api/opcua/read", json={"sid": sid, "node_ids": [tag["node_id"], "ns=2;s=nope"]}).json()
    assert r[0]["status"] == "Good" and r[1]["status"].startswith("Bad")


def test_export_shows_naming_drift(ua):
    c, url, sid, _ = ua
    gw = find(c, sid, None, "FactoryTalk Linx Gateway")
    ex = run_export(c, sid, gw["node_id"])
    paths = {t["path"] for t in ex["tags"]}
    assert "PRESS_01/PressFireCount" in paths and "PRESS_02/Press_Fire_Cnt" in paths
    assert "PRESS_02/Fault_Log/Fault_Log[0]/Station" in paths
    assert not ex["truncated"]


def test_bad_session_and_bad_endpoint(ua):
    c, url, sid, d = ua
    assert c.post("/api/opcua/browse", json={"sid": "nope"}).status_code == 404
    assert c.post("/api/opcua/connect", json={"url": "http://x"}).status_code == 400
    r = c.post("/api/opcua/connect", json={"url": f"127.0.0.1:{free_port()}"})
    assert r.status_code == 502
    assert "opcua.connect" in (d / "audit.log").read_text()


def test_tag_browser_is_admin_only(tmp_path):
    from ghostmap.web import auth

    auth.UserStore(tmp_path).set("op", "viewer-password", "viewer")
    with TestClient(create_app(data_dir=str(tmp_path)), headers=H) as c:
        c.post("/api/login", json={"user": "op", "password": "viewer-password"})
        assert c.post("/api/opcua/connect", json={"url": "127.0.0.1"}).status_code == 403
        assert c.post("/api/opcua/endpoints", json={"url": "127.0.0.1"}).status_code == 403


def test_client_certificate_for_secure_endpoints(tmp_path):
    import asyncio

    key, cert = asyncio.run(opcua.ensure_client_certificate(tmp_path / "pki"))
    assert key.exists() and cert.exists()
    mtime = cert.stat().st_mtime
    asyncio.run(opcua.ensure_client_certificate(tmp_path / "pki"))  # reused, not regenerated
    assert cert.stat().st_mtime == mtime


def test_typing_demo_starts_simulated_gateway(tmp_path):
    with TestClient(create_app(data_dir=str(tmp_path), demo_opcua_port=free_port()), headers=H) as c:
        assert c.get("/api/info").json()["demo_opcua"] is None  # not started until asked for
        r = c.post("/api/opcua/connect", json={"url": " Demo "}).json()
        assert r["url"].startswith("opc.tcp://127.0.0.1:") and r["sid"]
        assert c.get("/api/info").json()["demo_opcua"] == r["url"]
        assert c.post("/api/opcua/endpoints", json={"url": "demo"}).status_code == 200


def test_browse_follows_continuation_points():
    """Servers may return children in pages; the rest must be fetched with BrowseNext."""
    import asyncio

    from asyncua import ua

    def ref(name):
        r = ua.ReferenceDescription()
        r.NodeId = ua.NodeId(name, 2)
        r.BrowseName = ua.QualifiedName(name, 2)
        r.DisplayName = ua.LocalizedText(name)
        r.NodeClass = ua.NodeClass.Variable
        return r

    def result(names, cont):
        res = ua.BrowseResult()
        res.References = [ref(n) for n in names]
        res.ContinuationPoint = cont
        return res

    class FakeUaClient:
        async def browse(self, params):
            assert params.RequestedMaxReferencesPerNode == opcua.BROWSE_MAX_REFS
            return [result(["a", "b"], b"page2")]

        async def browse_next(self, params):
            return [result(["c"], None)] if params.ContinuationPoints == [b"page2"] else []

    b = opcua.UaBrowser("127.0.0.1")
    b.client.uaclient = FakeUaClient()
    kids = asyncio.run(b._browse_many(["ns=2;s=x"]))
    assert [k["name"] for k in kids[0]] == ["a", "b", "c"]


def test_dashboard_from_simulated_leveler(ua):
    c, url, sid, d = ua
    gw = find(c, sid, None, "FactoryTalk Linx Gateway")
    lev = find(c, sid, gw["node_id"], "LEVELER_01")
    job = c.post("/api/opcua/export", json={"sid": sid, "node_id": lev["node_id"]}).json()["job"]
    tags = run_export(c, sid, lev["node_id"])["tags"]
    rows = [{"path": t["path"], "node_id": t["node_id"], "type": t["variant_type"], "value": t["value"]} for t in tags]
    dash = c.post("/api/dashboards", json={"name": "Leveler 1", "endpoint": url, "tags": rows}).json()
    L = dash["layout"]
    areas = {a["title"]: a for a in L["areas"]}
    assert {"General", "Entry", "Leveler", "Hydraulics", "Comms", "Production", "Motion axes"} <= set(areas)
    assert areas["Comms"]["suggest_invert"]
    assert areas["Hydraulics"]["timers"] and areas["Production"]["counters"]
    axis = areas["Motion axes"]["axes"][0]
    assert axis["name"] == "Ax_Leveler_Roll" and "CIPAxisState" in axis["members"]
    assert L["summary"]["excluded"] == 101  # Local:1:I and the Recipe_Length array
    assert not any("Recipe" in a["id"] for a in L["areas"])
    assert c.get("/api/dashboards").json()[0]["id"] == dash["id"]
    assert L["machine"]["running"][0].endswith("Production.Line_Running")  # not Auto_Batch_Runout

    # Building from the server-side export job gives the same layout without uploading the tags.
    for _ in range(100):
        if c.get(f"/api/opcua/export/{job}").json()["status"] == "done":
            break
    by_job = c.post("/api/dashboards", json={"name": "Leveler 1b", "endpoint": url, "job": job}).json()
    assert by_job["layout"]["summary"] == L["summary"]
    c.delete(f"/api/dashboards/{by_job['id']}")

    vals = c.get(f"/api/dashboards/{dash['id']}/values").json()
    assert vals["ok"], vals
    estop = next(a for a in areas["General"]["alarms"] if a["name"] == "E_Stop_Flt")
    assert estop["node_id"] in vals["values"] and axis["members"]["ActualPosition"] in vals["values"]
    detail = c.get(f"/api/dashboards/{dash['id']}/axis", params={"name": "Ax_Leveler_Roll"}).json()
    assert detail["ok"] and detail["checked"] == 6
    assert "ActualPosition" in [m["name"] for m in detail["groups"]["Motion"]] and "Fault words" in detail["groups"]

    # Editing: tags are picked from the dashboard's own export, not typed in.
    found = c.get(f"/api/dashboards/{dash['id']}/tags", params={"q": "coil count"}).json()
    assert [t["path"].split("/")[-1] for t in found] == ["Coil_Count"]
    assert c.get(f"/api/dashboards/{dash['id']}/tags", params={"q": "estop"}).json()[0]["path"].endswith("E_Stop_Flt")
    edited = dict(L)
    gen = next(a for a in edited["areas"] if a["title"] == "General")
    gen["alarms"][0]["label"] = "Emergency stop pressed somewhere on the line"
    gen["values"].append({"name": "Coil_Count", "label": "Coils", "node_id": found[0]["node_id"]})
    r = c.post(f"/api/dashboards/{dash['id']}/layout", json={"layout": edited})
    assert r.status_code == 200, r.text
    assert any(v["label"] == "Coils" for a in r.json()["layout"]["areas"] for v in a["values"])
    gen["values"].append({"name": "x", "label": "x", "node_id": "ns=2;s=NotDiscovered"})
    assert c.post(f"/api/dashboards/{dash['id']}/layout", json={"layout": edited}).status_code == 400

    # Rebuild from the stored export: area edits go, the running tag stays.
    rb = c.post(f"/api/dashboards/{dash['id']}/rebuild").json()
    assert not any(v["label"] == "Coils" for a in rb["layout"]["areas"] for v in a["values"])
    assert rb["layout"]["machine"]["running"] == L["machine"]["running"]

    hidden = areas["General"]["alarms"][-1]["node_id"]
    r = c.post(f"/api/dashboards/{dash['id']}/overrides", json={"invert": {areas["Comms"]["id"]: True}, "hidden": [hidden]})
    assert r.json()["overrides"]["invert"] == {areas["Comms"]["id"]: True}
    assert (d / "dashboards" / f"{dash['id']}.json").exists()

    assert c.delete(f"/api/dashboards/{dash['id']}").json() == {"ok": True}
    assert c.get(f"/api/dashboards/{dash['id']}").status_code == 404
    assert not (d / "dashboards" / f"{dash['id']}.tags.json").exists()


def test_dashboard_unreachable_gateway_and_bad_input(tmp_path):
    with TestClient(create_app(data_dir=str(tmp_path), demo_opcua=False), headers=H) as c:
        assert c.post("/api/dashboards", json={"name": "x", "endpoint": "1.2.3.4", "tags": []}).status_code == 400
        rows = [{"path": "A/Flt", "node_id": "ns=2;s=FAULT.A.Flt", "type": "Boolean", "value": "false"}]
        dash = c.post("/api/dashboards", json={"name": "x", "endpoint": f"127.0.0.1:{free_port()}", "tags": rows}).json()
        v = c.get(f"/api/dashboards/{dash['id']}/values").json()
        assert v["ok"] is False and v["error"]
        # Requests queued behind a dead gateway get that failure back, not one connect attempt each.
        for _ in range(3):
            assert c.get(f"/api/dashboards/{dash['id']}/values").json()["ok"] is False
        assert c.get(f"/api/dashboards/{dash['id']}/health").json()["failed"] == 1
        assert c.get("/api/dashboards/..%2Fusers").status_code == 404


def test_one_bad_node_id_does_not_break_the_dashboard(ua):
    """A mistyped NodeId in a hand-edited CSV: that tag is unreadable, the rest read, the session stays up."""
    c, url, sid, _ = ua
    gw = find(c, sid, None, "FactoryTalk Linx Gateway")
    lev = find(c, sid, gw["node_id"], "LEVELER_01")
    rows = [{"path": t["path"], "node_id": t["node_id"], "type": t["variant_type"], "value": t["value"]}
            for t in run_export(c, sid, lev["node_id"])["tags"]]
    rows.append({"path": "Faults/Bogus_Flt", "node_id": "ns=2;x=bogus", "type": "Boolean", "value": False})
    did = c.post("/api/dashboards", json={"name": "Typo", "endpoint": url, "tags": rows}).json()["id"]
    import time
    for _ in range(3):
        v = c.get(f"/api/dashboards/{did}/values").json()
        time.sleep(1.1)
    assert v["ok"] and "ns=2;x=bogus" in v["bad"] and len(v["values"]) > 20
    h = c.get(f"/api/dashboards/{did}/health").json()
    assert h["drops"] == 0 and h["reconnects"] == 0 and h["bad_status"]["ns=2;x=bogus"] == "BadNodeIdInvalid"


def test_dashboards_viewers_watch_admins_edit(tmp_path, monkeypatch):
    from ghostmap.web import auth

    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    users = auth.UserStore(tmp_path)
    users.set("boss", "adminpassword", "admin")
    users.set("op", "viewerpassword", "viewer")
    rows = [{"path": "A/Flt", "node_id": "ns=2;s=FAULT.A.Flt", "type": "Boolean", "value": "false"}]
    with TestClient(create_app(data_dir=str(tmp_path), demo_opcua=False), headers=H) as c:
        c.post("/api/login", json={"user": "boss", "password": "adminpassword"})
        dash = c.post("/api/dashboards", json={"name": "x", "endpoint": "demo", "tags": rows}).json()
        c.post("/api/logout")
        c.post("/api/login", json={"user": "op", "password": "viewerpassword"})
        assert c.get(f"/api/dashboards/{dash['id']}").status_code == 200
        assert c.post(f"/api/dashboards/{dash['id']}/overrides", json={}).status_code == 403
        assert c.delete(f"/api/dashboards/{dash['id']}").status_code == 403
        assert c.post("/api/dashboards", json={"name": "y", "endpoint": "demo", "tags": rows}).status_code == 403


def test_comms_health_alarm_check_and_reconnects(tmp_path, monkeypatch):
    """Coverage, heartbeat, drops and reconnects, the alarm check and the debug log, against the simulator."""
    import io
    import time
    import zipfile

    from ghostmap.analysis import dashboard as gen
    from ghostmap.web import dashboards as live_mod

    monkeypatch.setattr(live_mod, "HEARTBEAT_STALE_S", 2)
    monkeypatch.setattr(gen, "HEARTBEAT_STALE_S", 2)
    app = create_app(data_dir=str(tmp_path), demo_opcua=True, demo_opcua_port=free_port())
    with TestClient(app, headers=H) as c:
        url = c.get("/api/info").json()["demo_opcua"]
        sid = c.post("/api/opcua/connect", json={"url": url}).json()["sid"]
        gw = find(c, sid, None, "FactoryTalk Linx Gateway")
        lev = find(c, sid, gw["node_id"], "LEVELER_01")
        rows = [{"path": t["path"], "node_id": t["node_id"], "type": t["variant_type"], "value": t["value"]}
                for t in run_export(c, sid, lev["node_id"])["tags"]]
        dash = c.post("/api/dashboards", json={"name": "Leveler", "endpoint": url, "tags": rows}).json()
        did = dash["id"]
        assert dash["layout"]["machine"]["heartbeat"][0].endswith("]Heartbeat")
        sim = app.state.demo_ua["server"]

        v = c.get(f"/api/dashboards/{did}/values").json()
        h = v["health"]
        assert v["ok"] and h["good"] == h["total"] > 0 and h["latency_ms"] is not None and not h["bad_by_plc"]
        assert dash["layout"]["machine"]["heartbeat"][0] in v["values"]  # machine tags are read too

        r = c.get(f"/api/dashboards/{did}/verify").json()
        assert r["ok"] and r["summary"]["alarms"] > 20 and r["summary"]["readable"] == r["summary"]["alarms"]
        codes = {f["code"] for f in r["findings"]}
        assert "area.mostly_on" in codes  # the Comms OK bits, until the area is flipped
        assert not [f for f in r["findings"] if f["severity"] == "error"]

        # FT Linx loses the PLC for one area: those tags come back BadCommunicationError.
        n = c.portal.call(sim.fault_comms, "Hydraulics")
        time.sleep(1.1)
        h = c.get(f"/api/dashboards/{did}/values").json()["health"]
        assert h["bad_by_plc"] == {"LEVELER_01": n} and h["good"] == h["total"] - n
        r = c.get(f"/api/dashboards/{did}/verify").json()
        bad = [f for f in r["findings"] if f["code"] == "alarm.unreadable"]
        assert len(bad) == n and "BadCommunicationError" in bad[0]["message"] and bad[0]["hint"]
        c.portal.call(sim.fault_comms, "Hydraulics", False)

        # The connection drops under us: the next read re-opens it and still returns data.
        live = app.state.live
        c.portal.call(live._conn(live._s[did])["browser"].client.disconnect)
        time.sleep(1.1)
        v = c.get(f"/api/dashboards/{did}/values").json()
        assert v["ok"], v
        assert v["health"]["drops"] == 1 and v["health"]["reconnects"] == 1
        kinds = [e["kind"] for e in c.get(f"/api/dashboards/{did}/health").json()["events"]]
        assert kinds.index("reconnected") < kinds.index("drop") and "coverage" in kinds  # newest first

        # A frozen heartbeat is reported even though every tag still reads Good.
        sim.freeze_heartbeat()
        for _ in range(4):
            time.sleep(1.1)
            h = c.get(f"/api/dashboards/{did}/values").json()["health"]
        assert h["heartbeat"]["frozen"] and h["good"] == h["total"]
        assert "heartbeat.frozen" in {f["code"] for f in c.get(f"/api/dashboards/{did}/verify").json()["findings"]}
        sim.freeze_heartbeat(False)

        # The Tag Browser session also survives a drop.
        c.portal.call(app.state.ua_sessions[sid][0].client.disconnect)
        assert c.post("/api/opcua/browse", json={"sid": sid, "node_id": None}).status_code == 200

        # Browser errors and the drops end up in the debug log and the bundle.
        assert c.post("/api/clientlog", json={"message": "TypeError: x is undefined", "page": "opcua"}).json()["ok"]
        text = c.get("/api/debug/log").text
        assert "drop" in text and "TypeError: x is undefined" in text and "re-opening" in text
        z = zipfile.ZipFile(io.BytesIO(c.get("/api/debug/bundle").content))
        assert "info.json" in z.namelist() and "logs/ghostmap.log" in z.namelist()
        assert '"drops": 1' in z.read("info.json").decode()


def test_collector_records_first_out_on_one_shared_session(tmp_path, monkeypatch):
    """With nobody watching, the collector reads both dashboards through one gateway session and records
    the E-stop as the first-out of the stop it causes."""
    import time

    from ghostmap.web import collector as collector_mod

    monkeypatch.setattr(collector_mod, "RESCAN_S", 0.2)
    app = create_app(data_dir=str(tmp_path), demo_opcua=True, demo_opcua_port=free_port())
    with TestClient(app, headers=H) as c:
        sim = app.state.demo_ua["server"]
        sim.auto_faults = False
        url = c.get("/api/info").json()["demo_opcua"]
        sid = c.post("/api/opcua/connect", json={"url": url}).json()["sid"]
        gw = find(c, sid, None, "FactoryTalk Linx Gateway")
        lev = find(c, sid, gw["node_id"], "LEVELER_01")
        rows = [{"path": t["path"], "node_id": t["node_id"], "type": t["variant_type"], "value": t["value"]}
                for t in run_export(c, sid, lev["node_id"])["tags"]]
        did = c.post("/api/dashboards", json={"name": "Leveler", "endpoint": url, "tags": rows}).json()["id"]
        dash = c.get(f"/api/dashboards/{did}").json()
        comms = next(a["id"] for a in dash["layout"]["areas"] if a["id"].endswith("Comms"))
        c.post(f"/api/dashboards/{did}/overrides", json={"invert": {comms: True}, "hidden": []})
        faults_only = [r for r in rows if "/FAULT/" in r["path"]]
        other = c.post("/api/dashboards", json={"name": "Faults", "endpoint": url, "tags": faults_only}).json()["id"]

        def wait(cond, secs=10):
            end = time.time() + secs
            while time.time() < end:
                if cond():
                    return True
                time.sleep(0.2)
            return False

        live = app.state.live
        assert wait(lambda: did in live._s and other in live._s and live.sessions()[0]["open"])
        assert len(live.sessions()) == 1 and live.sessions()[0]["dashboards"] == 2
        assert c.get(f"/api/dashboards/{did}/health").json()["shared_with"] == 1
        assert wait(lambda: app.state.history.current(did)["stop"] is None)  # clear before the test fault

        c.portal.call(sim.set_fault, "E_Stop_Flt", True)
        time.sleep(1.5)
        c.portal.call(sim.set_fault, "Roll_Drive_Flt", True)
        time.sleep(1.5)
        cur = c.get(f"/api/dashboards/{did}/values").json()
        assert cur["recording"] and len(cur["first_out"]) == 1 and cur["first_out"][0].endswith("E_Stop_Flt")
        c.portal.call(sim.set_fault, "E_Stop_Flt", False)
        c.portal.call(sim.set_fault, "Roll_Drive_Flt", False)
        assert wait(lambda: app.state.history.current(did)["stop"] is None)

        h = c.get(f"/api/dashboards/{did}/history", params={"hours": 1}).json()
        stop = next(s for s in h["stops"] if s["first_known"])
        assert stop["alarms"] == 2 and stop["tie"] == 1 and stop["end"] is not None
        assert [f["key"] for f in stop["first_out"]] == cur["first_out"]
        assert h["summary"]["first_out"][0]["key"].endswith("E_Stop_Flt")
        assert "E_Stop_Flt,2," in c.get(f"/api/dashboards/{did}/history.csv", params={"hours": 1}).text
        assert c.get(f"/api/dashboards/{did}/history", params={"hours": 0}).status_code == 400

        # Deleting a dashboard stops its collection; the gateway session stays for the other one.
        c.delete(f"/api/dashboards/{other}")
        assert wait(lambda: live.sessions() and live.sessions()[0]["dashboards"] == 1)

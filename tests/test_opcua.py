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
    ex = c.post("/api/opcua/export", json={"sid": sid, "node_id": gw["node_id"]}).json()
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

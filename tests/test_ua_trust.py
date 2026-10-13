"""Gateway certificate pinning (ghostmap/ua_trust.py): trust on first use, refuse secure sessions to a changed
certificate, warn on Security None sessions, until an admin accepts it."""

import pytest
from fastapi.testclient import TestClient

from ghostmap.ua_trust import CertificateChanged, CertTrust, server_key
from ghostmap.web.app import create_app
from test_opcua import H, free_port, leveler_dashboard, wait_for  # tests/ is on sys.path (no __init__.py)

URL = "opc.tcp://Gateway01:4990"


def test_server_key_is_the_address():
    assert server_key("opc.tcp://Gateway01:4990/path") == "gateway01:4990"
    assert server_key("opc.tcp://10.0.0.5") == "10.0.0.5:4840"


def test_first_certificate_is_pinned_and_a_change_is_caught(tmp_path):
    t = CertTrust(tmp_path)
    assert t.check(URL, b"cert-A", secure=False) is None  # first use: pinned
    assert t.check(URL, b"cert-A", secure=True) is None
    assert t.warning(URL) is None

    warn = t.check(URL, b"cert-B", secure=False)  # unsigned session: keeps going, with a warning
    assert "gateway01:4990" in warn and "Gateway certificates" in warn
    with pytest.raises(CertificateChanged):
        t.check(URL, b"cert-B", secure=True)
    changed = t.warning(URL)
    assert changed and changed["sha256"] != t.servers["gateway01:4990"]["sha256"]

    # Only the certificate that was seen can be accepted, and the pins survive a restart.
    with pytest.raises(KeyError):
        t.accept("gateway01:4990", "0" * 64, "nelson")
    t.accept("gateway01:4990", changed["sha256"], "nelson")
    t2 = CertTrust(tmp_path)
    assert t2.servers["gateway01:4990"]["pinned_by"] == "accepted by nelson" and not t2.changed
    assert t2.check(URL, b"cert-B", secure=True) is None
    t2.forget("gateway01:4990")
    assert t2.list() == [] and t2.check(URL, b"cert-C", secure=True) is None


def test_gateway_with_a_new_certificate(tmp_path):
    """The demo gateway is pinned on first connect; replaced by one with another certificate (a reinstall), a
    secure session is refused, a Security None one reads with a warning, and an admin can accept it."""
    from ghostmap.sim.opcua_server import SimUaServer

    port = free_port()
    app = create_app(data_dir=str(tmp_path / "data"), demo_opcua=True, demo_opcua_port=port)
    with TestClient(app, headers=H) as c:
        url = c.get("/api/info").json()["demo_opcua"]
        r = c.post("/api/opcua/connect", json={"url": url})
        assert r.status_code == 200 and r.json()["cert_warning"] is None
        pins = c.get("/api/opcua/trust").json()
        assert len(pins) == 1 and pins[0]["pinned"]["pinned_by"] == "first connection" and not pins[0]["changed"]
        secure = {"url": url, "security": "Basic256Sha256", "mode": "SignAndEncrypt"}
        assert c.post("/api/opcua/connect", json=secure).status_code == 200
        did = leveler_dashboard(c, app)
        assert wait_for(lambda: c.get(f"/api/dashboards/{did}/values").json()["ok"])

        # The gateway is reinstalled: same address, new certificate.
        c.portal.call(app.state.demo_ua["server"].stop)
        app.state.demo_ua["server"] = c.portal.call(SimUaServer(port=port, cert_dir=tmp_path / "reinstalled").start)
        r = c.post("/api/opcua/connect", json=secure)
        assert r.status_code == 409 and "Gateway certificates" in r.json()["detail"]
        r = c.post("/api/opcua/connect", json={"url": url})
        assert r.status_code == 200 and "not the one Ghost Map pinned" in r.json()["cert_warning"]

        # Dashboards (Security None) keep reading after the drop, and say why they warn.
        def warned():
            v = c.get(f"/api/dashboards/{did}/values").json()
            return v["ok"] and v["health"]["cert_changed"]
        assert wait_for(warned, 15)
        ev = c.get(f"/api/dashboards/{did}/health").json()["events"]
        assert any(e["kind"] == "certificate" for e in ev)

        changed = c.get("/api/opcua/trust").json()[0]["changed"]
        assert c.post("/api/opcua/trust/accept", json={"key": "nope:1", "sha256": changed["sha256"]}).status_code == 404
        pins = c.post("/api/opcua/trust/accept", json={"key": pins[0]["key"], "sha256": changed["sha256"]}).json()
        assert pins[0]["pinned"]["sha256"] == changed["sha256"] and pins[0]["changed"] is None
        assert c.post("/api/opcua/connect", json=secure).status_code == 200
        assert not c.get(f"/api/dashboards/{did}/health").json()["cert_changed"]

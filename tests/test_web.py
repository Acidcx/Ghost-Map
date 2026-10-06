import time

import pytest
from fastapi.testclient import TestClient

from ghostmap.web import auth
from ghostmap.web.app import create_app
from ghostmap.web.auth import UserStore, client_allowed, parse_allow

H = {"X-Ghostmap": "1"}


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=str(tmp_path), demo=True), headers=H) as c:
        yield c


@pytest.fixture
def secured(tmp_path):
    users = UserStore(tmp_path)
    users.set("boss", "admin-password", "admin")
    users.set("op", "viewer-password", "viewer")
    with TestClient(create_app(data_dir=str(tmp_path), demo=True), headers=H) as c:
        yield c


def login(c, user, password):
    return c.post("/api/login", json={"user": user, "password": password})


def test_index_and_static(client):
    assert "Ghost Map" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_scans_api(client):
    scans = client.get("/api/scans").json()
    assert {s["id"] for s in scans} == {"demo-today", "demo-baseline"}
    scan = client.get("/api/scans/demo-today").json()
    assert scan["switches"][0]["sys_name"] == "CELL1-SW01"
    assert scan["devices"][1]["identity"]["revision"]
    assert client.get("/api/scans/nope").status_code == 404
    assert client.get("/api/scans/..%2fetc").status_code == 404


def test_csv_and_diff(client):
    csv = client.get("/api/scans/demo-today/inventory.csv")
    assert csv.status_code == 200
    assert csv.text.splitlines()[0].startswith("ip,mac,mac_vendor,vendor,product")
    d = client.get("/api/diff", params={"old": "demo-baseline", "new": "demo-today"}).json()
    assert d["replaced"][0]["ip"] == "192.168.1.22"


def test_scan_validation(client):
    assert client.post("/api/scans", json={}).status_code == 400
    assert client.post("/api/scans", json={"targets": ["10.0.0.0/8"]}).status_code == 400
    assert client.post("/api/scans", json={"targets": ["x"], "rate": 0}).status_code == 422


def test_scan_job_runs(client):
    r = client.post("/api/scans", json={"targets": ["127.0.0.1"], "timeout": 0.2, "label": "t",
                                        "snmp": {"version": "2c", "community": "secret"}})
    job = r.json()["job"]
    for _ in range(50):
        j = client.get(f"/api/jobs/{job}").json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    scan = client.get(f"/api/scans/{j['scan_id']}").json()
    assert scan["params"]["label"] == "t"
    assert "secret" not in str(scan)  # credentials never persisted


def test_relative_urls_for_ixon_proxy(client):
    html = client.get("/").text
    assert 'href="static/' in html and 'src="static/' in html
    assert '"/static' not in html and '"/api' not in client.get("/static/app.js").text


def test_security_headers(client):
    r = client.get("/api/scans")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/docs").status_code == 404  # no API explorer


def test_state_change_needs_header(tmp_path):
    with TestClient(create_app(data_dir=str(tmp_path))) as c:
        assert c.post("/api/demo").status_code == 403
        assert c.post("/api/demo", headers=H).status_code == 200


def test_open_when_no_users(client):
    assert client.get("/api/info").json()["auth"] is False
    assert client.post("/api/demo").status_code == 200


def test_login_required_once_users_exist(secured):
    assert secured.get("/api/scans").status_code == 401
    r = secured.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "login"
    assert secured.get("/login").status_code == 200
    assert secured.get("/static/login.js").status_code == 200
    assert login(secured, "boss", "wrong-password").status_code == 401
    r = login(secured, "boss", "admin-password")
    assert r.json()["role"] == "admin"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert secured.get("/api/scans").status_code == 200
    assert secured.get("/api/info").json()["user"] == "boss"
    secured.post("/api/logout")
    assert secured.get("/api/scans").status_code == 401


def test_viewer_cannot_scan_or_probe(secured):
    login(secured, "op", "viewer-password")
    assert secured.get("/api/scans").status_code == 200
    assert secured.post("/api/scans", json={"targets": ["127.0.0.1"]}).status_code == 403
    assert secured.post("/api/demo").status_code == 403
    assert secured.get("/api/probe/127.0.0.1").status_code == 403
    assert secured.delete("/api/scans/demo-today").status_code == 403


def test_lockout_and_audit(secured, tmp_path):
    for _ in range(auth.MAX_FAILURES):
        assert login(secured, "boss", "nope-nope-nope").status_code == 401
    assert login(secured, "boss", "admin-password").status_code == 429  # locked even with the right password
    log = (tmp_path / "audit.log").read_text()
    assert log.count("login.failed") == auth.MAX_FAILURES and "login.locked" in log
    assert "admin-password" not in log


def test_audit_records_scans(secured, tmp_path):
    login(secured, "boss", "admin-password")
    secured.post("/api/demo")
    secured.delete("/api/scans/demo-baseline")
    log = (tmp_path / "audit.log").read_text()
    assert "\tboss\tdemo.load" in log and "scan.delete\tdemo-baseline" in log


def test_users_file_has_no_plaintext(tmp_path):
    UserStore(tmp_path).set("boss", "admin-password", "admin")
    assert "admin-password" not in (tmp_path / "users.json").read_text()
    with pytest.raises(ValueError):
        UserStore(tmp_path).set("x", "short", "admin")
    with pytest.raises(ValueError):
        UserStore(tmp_path).set("x", "long-enough-pw", "root")


def test_client_allow_list(tmp_path):
    allow = parse_allow(["192.168.1.1", "10.0.5.0/24"])
    assert client_allowed("127.0.0.1", allow) and client_allowed("::1", allow)
    assert client_allowed("192.168.1.1", allow) and client_allowed("10.0.5.77", allow)
    assert client_allowed("::ffff:192.168.1.1", allow)
    assert not client_allowed("192.168.1.2", allow) and not client_allowed("testclient", allow)
    with pytest.raises(ValueError):
        parse_allow(["not-an-ip"])
    with TestClient(create_app(data_dir=str(tmp_path), allow=allow)) as c:  # TestClient's peer is "testclient"
        assert c.get("/api/scans").status_code == 403

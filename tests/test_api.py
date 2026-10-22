"""The web API's rules, checked on every route so a new endpoint can't quietly skip them: state changes need the
X-Ghostmap header, every route needs a login once users exist, viewers only read (and not everything), every
answer carries the security headers, and saved secrets never come back out."""

import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from ghostmap.web import auth
from ghostmap.web.app import SECURITY_HEADERS, create_app
from ghostmap.web.auth import UserStore

H = {"X-Ghostmap": "1"}
SAMPLE = {"dash_id": "nope", "scan_id": "demo-today", "job_id": "nope", "item_id": "nope", "name": "arp", "ip": "127.0.0.1"}
# What a viewer may change: their own session and the browser error log.
VIEWER_WRITES = {("POST", "/api/login"), ("POST", "/api/logout"), ("POST", "/api/clientlog")}
# What a viewer may not read: user accounts, connection settings, gateway certificates, tag lists and exports,
# logs, and active probing.
ADMIN_READS = {"/api/users", "/api/opcua/trust", "/api/opcua/export/{job_id}", "/api/dashboards/{dash_id}/tags", "/api/export",
               "/api/tsc/config", "/api/netmon/config", "/api/debug/log", "/api/debug/bundle", "/api/probe/{ip}/ping",
               "/api/tsclogs", "/api/tsclogs/file", "/api/tsclogs/download"}


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)


def routes(app):
    out = []
    for r in app.routes:
        for m in sorted(getattr(r, "methods", None) or ()):
            if m != "HEAD" and r.path.startswith("/api/"):
                out.append((m, r.path))
    assert len(out) > 60
    return out


def url(path):
    return re.sub(r"\{(\w+)\}", lambda m: SAMPLE[m.group(1)], path)


def secure_headers(r):
    missing = [k for k in SECURITY_HEADERS if k.lower() not in r.headers]
    assert not missing, f"{r.request.method} {r.request.url.path} ({r.status_code}) lacks {missing}"


@pytest.fixture
def secured(tmp_path):
    users = UserStore(tmp_path)
    users.set("boss", "admin-password", "admin")
    users.set("op", "viewer-password", "viewer")
    app = create_app(data_dir=str(tmp_path), demo=True, demo_opcua=False, collect=False)
    with TestClient(app, headers=H) as c:
        yield app, c


def test_every_state_change_needs_the_header(tmp_path):
    app = create_app(data_dir=str(tmp_path), demo_opcua=False, collect=False)  # no users: everything open
    with TestClient(app) as c:
        writes = [(m, p) for m, p in routes(app) if m != "GET"]
        for m, p in writes:
            r = c.request(m, url(p), json={})
            assert r.status_code == 403 and r.json()["detail"] == "missing X-Ghostmap header", (m, p, r.status_code)
            secure_headers(r)
        assert len(writes) > 30


def test_every_route_needs_a_login(secured):
    app, c = secured
    for m, p in routes(app):
        r = c.request(m, url(p), json={} if m != "GET" else None)
        if p == "/api/login":
            assert r.status_code in (401, 422)  # open, but needs a right password
        else:
            assert r.status_code == 401, (m, p, r.status_code)
        secure_headers(r)


def test_what_a_viewer_can_do(secured):
    app, c = secured
    assert c.post("/api/login", json={"user": "op", "password": "viewer-password"}).status_code == 200
    refused_reads = set()
    for m, p in routes(app):
        if (m, p) in VIEWER_WRITES:
            continue
        r = c.request(m, url(p), json={} if m != "GET" else None)
        secure_headers(r)
        if m != "GET":
            assert r.status_code == 403, (m, p, r.status_code)
        elif r.status_code == 403:
            refused_reads.add(p)
        else:
            assert r.status_code in (200, 404, 422), (m, p, r.status_code)  # read, unknown id, or a missing parameter
    # A new GET endpoint has to be put on one side or the other here.
    assert refused_reads == ADMIN_READS
    assert c.get("/api/info").json()["role"] == "viewer"  # still logged in after all that


def test_saved_secrets_never_come_back(secured):
    app, c = secured
    c.post("/api/login", json={"user": "boss", "password": "admin-password"})
    secrets = ["Sql-Secret-1", "Community-Secret-2", "Auth-Key-Secret-3", "Priv-Key-Secret-4", "admin-password"]
    r = c.post("/api/tsc/config", json={"server": "sql01", "database": "TSC", "username": "ghostmap_ro", "password": secrets[0]})
    assert r.status_code == 200
    r = c.post("/api/netmon/config", json={"switches": ["192.0.2.10"], "version": "2c", "community": secrets[1]})
    assert r.status_code == 200
    r = c.post("/api/netmon/config", json={"switches": ["192.0.2.10"], "version": "3", "username": "gm", "auth_key": secrets[2],
                                           "priv_key": secrets[3]})
    assert r.status_code == 200
    seen = []
    for p in ("/api/tsc/config", "/api/tsc/status", "/api/netmon/config", "/api/netmon/status", "/api/users", "/api/info",
              "/api/export", "/api/debug/log"):
        r = c.get(p)
        assert r.status_code == 200, p
        seen.append(r.text)
    with zipfile.ZipFile(io.BytesIO(c.get("/api/debug/bundle").content)) as z:
        seen += [z.read(n).decode("utf-8", "replace") for n in z.namelist()]
    blob = "\n".join(seen)
    for s in secrets:
        assert s not in blob, s
    assert json.loads(c.get("/api/tsc/config").text)["has_password"] is True
    # The files on disk don't hold them in the clear either (sealed with DPAPI or the local key).
    root = app.state.tsc.root
    for f in ("tsc.json", "users.json"):
        text = (root / f).read_text(encoding="utf-8")
        assert all(s not in text for s in secrets), f

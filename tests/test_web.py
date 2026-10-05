import time

import pytest
from fastapi.testclient import TestClient

from ghostmap.web.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=str(tmp_path), demo=True)) as c:
        yield c


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

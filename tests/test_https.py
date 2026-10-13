"""HTTPS for the web UI (ghostmap/tls.py, web --https, service install --https)."""

import datetime as dt
import ssl
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from ghostmap import cli, tls, winservice
from ghostmap.web import auth
from ghostmap.web.app import create_app
from test_opcua import free_port  # tests/ is on sys.path (no __init__.py)

H = {"X-Ghostmap": "1"}


def test_self_signed_certificate_is_made_once_and_renewed(tmp_path, monkeypatch):
    monkeypatch.setattr(tls.socket, "gethostname", lambda: "HMI-LEVELER")
    first = tls.ensure(tmp_path)
    assert first["made"] and first["self_signed"] and first["cert"].endswith("ghostmap.crt")
    assert {"HMI-LEVELER", "localhost", "127.0.0.1", "::1"} <= set(first["names"])
    assert len(first["sha256"].split(":")) == 32
    left = first["not_after"] - time.time()
    assert tls.VALID_DAYS - 2 < left / 86400 <= tls.VALID_DAYS
    if sys.platform != "win32":
        assert (tmp_path / "tls" / "ghostmap.key").stat().st_mode & 0o077 == 0  # only the owner reads the key

    again = tls.ensure(tmp_path)  # the next start uses the same one, so a trusted certificate stays trusted
    assert not again["made"] and again["sha256"] == first["sha256"]

    monkeypatch.setattr(tls.socket, "gethostname", lambda: "HMI-RENAMED")  # the PC was renamed: a new one
    renamed = tls.ensure(tmp_path)
    assert renamed["made"] and "HMI-RENAMED" in renamed["names"]

    cert, key = tls.make_self_signed(["HMI-RENAMED"], ["127.0.0.1"], days=10)  # about to expire: a new one
    (tmp_path / "tls" / "ghostmap.crt").write_bytes(cert)
    (tmp_path / "tls" / "ghostmap.key").write_bytes(key)
    assert tls.ensure(tmp_path)["made"]


def test_plant_certificate(tmp_path):
    cert, key = tls.make_self_signed(["ghostmap.plant.local"], ["10.1.2.3"])
    (tmp_path / "plant.crt").write_bytes(cert)
    (tmp_path / "plant.key").write_bytes(key)
    got = tls.ensure(tmp_path / "data", str(tmp_path / "plant.crt"), str(tmp_path / "plant.key"))
    assert got["cert"] == str(tmp_path / "plant.crt") and not got["made"] and "10.1.2.3" in got["names"]
    assert not (tmp_path / "data" / "tls").exists()
    with pytest.raises(ValueError, match="go together"):
        tls.ensure(tmp_path, str(tmp_path / "plant.crt"), None)
    with pytest.raises(ValueError, match="not found"):
        tls.ensure(tmp_path, str(tmp_path / "missing.crt"), str(tmp_path / "plant.key"))


@pytest.fixture
def https_server(tmp_path, monkeypatch):
    """Ghost Map served over HTTPS on a free port, as ``web --https`` runs it; yields (base url, tls info)."""
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    info = tls.ensure(tmp_path)
    auth.UserStore(tmp_path).set("boss", "admin-password", "admin")
    port = free_port()
    app = create_app(data_dir=str(tmp_path), demo_opcua=False, collect=False, tls=info)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None,
                                           ssl_certfile=info["cert"], ssl_keyfile=info["key"]))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    end = time.time() + 20
    while not server.started and time.time() < end:
        time.sleep(0.05)
    assert server.started
    yield f"https://127.0.0.1:{port}", info
    server.should_exit = True
    t.join(15)


def test_https_round_trip(https_server):
    base, info = https_server
    trust = ssl.create_default_context(cafile=info["cert"])  # trusting exactly Ghost Map's certificate
    with httpx.Client(base_url=base, verify=trust, headers=H) as c:
        assert c.get("/api/info").status_code == 401
        r = c.post("/api/login", json={"user": "boss", "password": "admin-password"})
        cookie = r.headers["set-cookie"].lower()
        assert r.status_code == 200 and "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        i = c.get("/api/info").json()  # the cookie works over HTTPS
        assert i["https"] and i["tls"]["sha256"] == info["sha256"] and i["user"] == "boss"
        assert r.headers.get("x-content-type-options") == "nosniff"

    # Unverified clients are refused by the client's own check, and plain HTTP gets no answer.
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{base}/api/info", verify=ssl.create_default_context())
    with pytest.raises(httpx.HTTPError):
        httpx.get(base.replace("https:", "http:") + "/api/info", timeout=5)


def test_cli_https_options(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: seen.update(cfg=self.config))
    port = str(free_port())
    assert cli.main(["--data-dir", str(tmp_path), "web", "--https", "--port", port, "--no-listen", "--no-collect"]) == 0
    err = capsys.readouterr().err
    assert f"https://127.0.0.1:{port}" in err and "SHA-256" in err and "new, self-signed" in err
    assert seen["cfg"].ssl_certfile == str(tmp_path / "tls" / "ghostmap.crt")
    assert seen["cfg"].ssl_keyfile == str(tmp_path / "tls" / "ghostmap.key")

    assert cli.main(["--data-dir", str(tmp_path), "web", "--cert", "x.crt", "--port", port]) == 2
    assert "--https" in capsys.readouterr().err

    args = winservice.service_args(tmp_path, "0.0.0.0", 8470, ["192.168.1.1"], https=True)
    assert args[-1] == "--https"
    args = winservice.service_args(tmp_path, "127.0.0.1", 8470, https=True, cert="plant.crt", key="plant.key")
    assert args[args.index("--cert") + 1].endswith("plant.crt") and "--key" in args
    assert "--https" not in winservice.service_args(tmp_path, "127.0.0.1", 8470)


def test_certificate_dates_are_utc():
    cert, _ = tls.make_self_signed(["x"], ["127.0.0.1"], days=1)
    d = tls.describe(cert)
    assert abs(d["not_after"] - (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).timestamp()) < 60

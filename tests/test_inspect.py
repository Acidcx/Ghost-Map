import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from ghostmap.analysis.inspect import verdict
from ghostmap.collectors import inspect as inspector
from ghostmap.protocols.enip import CMD_LIST_SERVICES, EnipError, build_list_services, build_list_services_response, parse_list_services
from ghostmap.web.app import create_app
from ghostmap.web.tsc import TscService

H = {"X-Ghostmap": "1"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=str(tmp_path), demo=True, demo_opcua=False), headers=H) as c:
        yield c


def test_list_services_round_trip():
    assert int.from_bytes(build_list_services()[:2], "little") == CMD_LIST_SERVICES
    s = parse_list_services(build_list_services_response("Communications", 0x0120))
    assert s == [{"name": "Communications", "version": 1, "flags": 0x0120,
                  "capabilities": ["CIP over TCP (explicit messaging)", "CIP class 0/1 over UDP (I/O connections)"]}]
    assert parse_list_services(build_list_services_response(flags=0x0020))[0]["capabilities"] == ["CIP over TCP (explicit messaging)"]
    with pytest.raises(EnipError):
        parse_list_services(b"\x63\x00" + bytes(22))  # a ListIdentity header is not a ListServices reply


def test_latency_stats():
    s = inspector.latency_stats([1.0, None, 3.0, 2.0])
    assert (s["sent"], s["lost"], s["min"], s["max"], s["avg"]) == (4, 1, 1.0, 3.0, 2.0)
    assert s["jitter"] == 1.5  # |1-3| and |3-2|
    assert inspector.latency_stats([None, None])["avg"] is None


def _result(samples, ports=(), identity=None):
    lat = inspector.latency_stats(list(samples))
    open_ports = [{"port": p, "service": n, "open": p in ports, "note": note if p in ports else ""} for p, n, note in inspector.PORTS]
    return {"ip": "10.0.0.5", "ports": open_ports, "identity": identity, "snmp": None,
            "latency": {**lat, "port": next((p for p in ports), None) if any(s is not None for s in samples) else None}}


def test_verdict_codes():
    codes = lambda r, ctx={}: [c["code"] for c in verdict(r, ctx)]  # noqa: E731
    assert codes(_result([None] * 10)) == ["inspect.unreachable"]
    assert codes(_result([0.5] * 10, ports=(443,))) == ["inspect.reachable"]
    assert codes(_result([0.5] * 10, ports=(44818,)))[0] == "inspect.no_identity"  # TCP open, UDP ListIdentity blocked
    bad = codes(_result([0.5, None, None, 20.0, 1.0, 30.0], ports=(44818, 23)))
    assert bad[0] == "inspect.loss" and {"inspect.slow", "inspect.jitter", "inspect.port_23"} <= set(bad)
    ident = {"socket_ip": "10.0.0.9", "state": 5, "state_name": "Major unrecoverable fault", "status_flags": [], "serial_hex": "B", "revision": "2.1"}
    ctx = {"history": [{"scan": "s2", "serial": "B", "revision": "2.1"}, {"scan": "s1", "serial": "A", "revision": "2.1"}],
           "port": {"name": "Gi1/3", "duplex": "half", "speed_mbps": 100, "in_errors": 4, "fcs_errors": 0, "late_collisions": 0,
                    "devices": 3, "is_uplink": False}}
    got = verdict(_result([0.4] * 10, ports=(44818,), identity=ident), ctx)
    assert [c["level"] for c in got] == sorted([c["level"] for c in got], key=["err", "warn", "info", "ok"].index)  # worst first
    assert {"inspect.major_fault", "inspect.half_duplex", "inspect.socket_ip", "inspect.swapped", "inspect.port_errors",
            "inspect.shared_port"} <= {c["code"] for c in got}
    assert all(c["hint"] for c in got if c["level"] in ("err", "warn"))


def test_inspect_the_demo_machine(client):
    r = client.post("/api/inspect", json={"ip": "192.168.1.20"}).json()
    assert r["simulated"] and r["context"]["port"]["duplex"] == "half"
    assert r["context"]["switch"]["ip"] and len(r["context"]["history"]) >= 2
    codes = {c["code"] for c in r["checks"]}
    assert {"inspect.half_duplex", "inspect.port_errors"} <= codes
    assert r["identity"]["product_name"] and r["services"][0]["capabilities"]
    hmi = client.post("/api/inspect", json={"ip": "192.168.1.40"}).json()
    assert "inspect.port_5900" in {c["code"] for c in hmi["checks"]}
    assert client.post("/api/inspect", json={"ip": "not an ip"}).status_code == 400


def test_live_ping_is_rate_limited(client):
    first = client.get("/api/probe/192.168.1.11/ping")
    assert first.status_code == 200 and first.json()["ms"] > 0
    assert client.get("/api/probe/192.168.1.11/ping").status_code == 429
    time.sleep(0.3)
    assert client.get("/api/probe/192.168.1.11/ping?port=0").status_code == 400


def test_list_services_and_rtt_against_a_local_listener(monkeypatch):
    async def main():
        async def handle(reader, writer):
            head = await reader.readexactly(24)
            if int.from_bytes(head[:2], "little") == CMD_LIST_SERVICES:
                writer.write(build_list_services_response(flags=0x0020, context=head[12:20]))
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(inspector, "ENIP_PORT", port)
        async with server:
            services = await inspector.list_services("127.0.0.1")
            rtt = await inspector.tcp_rtt("127.0.0.1", port)
        return services, rtt

    services, rtt = asyncio.run(main())
    assert services[0]["capabilities"] == ["CIP over TCP (explicit messaging)"]
    assert rtt is not None and rtt >= 0


def test_counters_answer_stale_then_refresh(tmp_path):
    svc = TscService(tmp_path)
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.05)
        return {"n": len(calls)}

    async def main():
        first = await svc._run("k", slow, max_age=0.01, stale_s=60)
        await asyncio.sleep(0.02)
        second = await svc._run("k", slow, max_age=0.01, stale_s=60)  # old: answered at once, refreshed behind
        await asyncio.gather(*svc._bg)
        third = await svc._run("k", slow, max_age=60, stale_s=60)
        return first, second, third

    first, second, third = asyncio.run(main())
    assert first == {"n": 1} and second == {"n": 1, "stale": True} and third == {"n": 2}

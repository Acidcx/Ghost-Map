import asyncio
import socket
import time

import pytest
from fastapi.testclient import TestClient

from ghostmap.analysis import passive as rules
from ghostmap.collectors.passive import PassiveMonitor
from ghostmap.protocols.passive import (PassiveError, build_bootp_request, build_syslog, build_trap_v1, build_trap_v2c,
                                        parse_bootp, parse_snmp_trap, parse_syslog)
from ghostmap.web.app import create_app

H = {"X-Ghostmap": "1"}
RA_MAC = "00:00:bc:ee:00:60"


def test_bootp_and_dhcp_requests():
    b = parse_bootp(build_bootp_request(RA_MAC, xid=7))
    assert (b["request"], b["mac"], b["xid"], b["dhcp_type"]) == (True, RA_MAC, 7, None)
    d = parse_bootp(build_bootp_request(RA_MAC, dhcp=True, hostname="AENTR", vendor_class="Rockwell"))
    assert (d["dhcp_type"], d["hostname"], d["vendor_class"]) == ("DISCOVER", "AENTR", "Rockwell")
    with pytest.raises(PassiveError):
        parse_bootp(b"\x01" * 100)


def test_syslog_cisco_style():
    s = parse_syslog(build_syslog("88: %LINK-3-UPDOWN: Interface FastEthernet1/2, changed state to down", severity=3))
    assert (s["severity"], s["mnemonic"]) == ("error", "LINK-3-UPDOWN")
    ev = rules.syslog_event(s, "SW1")
    assert (ev["code"], ev["level"]) == ("passive.link_down", "warn") and "FastEthernet1/2" in ev["text"] and ev["hint"]
    flap = rules.syslog_event(parse_syslog(build_syslog("%SW_MATM-4-MACFLAP_NOTIF: Host 0000.bc11.2233 in vlan 10 is flapping")), "SW1")
    assert (flap["code"], flap["level"]) == ("passive.mac_flap", "err")
    other = rules.syslog_event(parse_syslog(b"plain text, no priority"), "PC")
    assert other["code"] == "passive.syslog" and other["level"] == "info"


def test_snmp_traps_v1_and_v2c():
    v2 = parse_snmp_trap(build_trap_v2c("1.3.6.1.6.3.1.1.5.3", [("1.3.6.1.2.1.31.1.1.1.1.3", "Fa1/2"),
                                                                ("1.3.6.1.2.1.31.1.1.1.18.3", "VFD")], uptime_ticks=12345, community="secret"))
    assert (v2["version"], v2["kind"], v2["trap"], v2["uptime_s"]) == ("v2c", "trap", "linkDown", 123)
    assert "secret" not in repr(v2)  # the community is a password: never kept
    ev = rules.trap_event(v2, "SW1")
    assert ev["code"] == "passive.link_down" and "Fa1/2 (VFD)" in ev["text"]
    v1 = parse_snmp_trap(build_trap_v1("1.3.6.1.4.1.9", "192.168.1.2", generic=0))
    assert (v1["version"], v1["trap"], v1["agent"]) == ("v1", "coldStart", "192.168.1.2")
    assert rules.trap_event(v1, "SW1")["code"] == "passive.restart"
    with pytest.raises(PassiveError):
        parse_snmp_trap(b"\x30\x03\x02\x01")


def test_arp_changes():
    known = {"00:00:bc:aa:00:10": "1756-EN2T (10.0.0.10)"}
    assert rules.arp_changes({}, {"10.0.0.10": "00:00:bc:aa:00:10"}, known) == []  # first read is the baseline
    prev = {"10.0.0.10": "00:00:bc:aa:00:10"}
    now = {**prev, "10.0.0.77": "00:0e:c6:12:34:77"}
    assert [e["code"] for e in rules.arp_changes(prev, now, known)] == ["passive.arp_unknown"]
    swap = rules.arp_changes(now, {**now, "10.0.0.77": "3c:52:82:11:22:33"}, known)
    assert [(e["code"], e["level"]) for e in swap] == [("passive.ip_conflict", "err")]


def test_bootp_from_a_known_device_is_an_error():
    msg = parse_bootp(build_bootp_request(RA_MAC))
    assert rules.bootp_event(msg, {}, {})["code"] == "passive.bootp"
    ev = rules.bootp_event(msg, {RA_MAC: "1734-AENTR (10.0.0.30)"}, {RA_MAC: "10.0.0.30"})
    assert (ev["code"], ev["level"], ev["ip"]) == ("passive.bootp_known", "err", "10.0.0.30")


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_listeners_hear_real_packets_and_leave_a_busy_port_alone(tmp_path):
    ports = {"bootp": _free_udp_port(), "syslog": _free_udp_port(), "traps": _free_udp_port()}
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("0.0.0.0", ports["traps"]))  # another trap receiver already has the port

    async def main():
        reads = iter([{"10.0.0.10": "00:00:bc:aa:00:10"}] * 1000)
        mon = PassiveMonitor(tmp_path, arp_reader=lambda: next(reads), ports=ports)
        await mon.start()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.sendto(build_bootp_request(RA_MAC), ("127.0.0.1", ports["bootp"]))
        tx.sendto(build_syslog("%LINK-3-UPDOWN: Interface Gi1/1, changed state to down"), ("127.0.0.1", ports["syslog"]))
        tx.close()
        for _ in range(50):
            if mon.status["bootp"]["packets"] and mon.status["syslog"]["packets"]:
                break
            await asyncio.sleep(0.05)
        snap = mon.snapshot()
        await mon.stop()
        return snap

    snap = asyncio.run(main())
    taken.close()
    by = {x["name"]: x for x in snap["listeners"]}
    assert by["bootp"]["state"] == "listening" and by["bootp"]["packets"] == 1
    assert by["traps"]["state"] == "error" and "in use" in by["traps"]["error"]
    assert {e["code"] for e in snap["events"]} >= {"passive.bootp", "passive.link_down"}
    assert any(d["mac"] == RA_MAC for d in snap["devices"]) and any(d["ip"] == "10.0.0.10" for d in snap["devices"])


def test_a_flood_is_counted_not_listed(tmp_path):
    mon = PassiveMonitor(tmp_path)
    t = time.time() // 60 * 60
    for i in range(100):
        mon.feed("syslog", build_syslog("%LINK-3-UPDOWN: Interface Fa1/1, changed state to down"), "10.0.0.2", at=t + i * 0.1)
    codes = [e["code"] for e in mon.events]
    assert codes.count("passive.link_down") == 60 and codes.count("passive.flood") == 1


def test_live_api_in_the_demo(tmp_path):
    with TestClient(create_app(data_dir=str(tmp_path), demo=True, demo_opcua=False, listen=True), headers=H) as c:
        d = c.get("/api/live").json()
        assert d["simulated"] and d["listen"] and d["seq"] > 0
        codes = {e["code"] for e in d["events"]}
        assert {"passive.bootp", "passive.link_down", "passive.duplex"} <= codes
        assert all(x["state"] == "listening" for x in d["listeners"])
        assert c.get(f"/api/live?since={d['seq']}").json()["events"] == []
        off = c.post("/api/live/listeners/syslog", json={"enabled": False}).json()
        assert next(x for x in off["listeners"] if x["name"] == "syslog")["state"] == "off"
        assert c.post("/api/live/listeners/nope", json={"enabled": False}).status_code == 404
    assert '"syslog": false' in (tmp_path / "live.json").read_text()


def test_listeners_are_off_unless_asked(tmp_path):
    with TestClient(create_app(data_dir=str(tmp_path), demo=True, demo_opcua=False), headers=H) as c:
        d = c.get("/api/live").json()
        assert not d["listen"] and not d["running"] and d["events"] == []

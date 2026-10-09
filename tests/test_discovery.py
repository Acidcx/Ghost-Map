import asyncio
import socket

import pytest

from ghostmap.collectors.discovery import discover, expand_targets, probe
from ghostmap.protocols.enip import build_list_identity_response
from ghostmap.sim.responder import serve


def test_expand_targets_forms():
    assert expand_targets(["10.0.0.0/30"]) == ["10.0.0.1", "10.0.0.2"]
    assert expand_targets(["10.0.0.5"]) == ["10.0.0.5"]
    assert expand_targets(["10.0.0.1-3"]) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert expand_targets(["10.0.0.9-10.0.0.10", "10.0.0.10"]) == ["10.0.0.9", "10.0.0.10"]


def test_expand_targets_limits():
    with pytest.raises(ValueError):
        expand_targets(["10.0.0.0/16"], max_hosts=1024)
    with pytest.raises(ValueError):
        expand_targets(["10.0.0.5-10.0.0.1"])


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _reply(ip, name, serial):
    return lambda ctx: build_list_identity_response(
        ip=ip, vendor_id=1, device_type=0x0E, product_code=55, revision=(33, 11), status=0x0060,
        serial=serial, product_name=name, context=ctx)


def test_discover_against_simulated_devices():
    port = _free_udp_port()

    async def run():
        transports = await serve({"127.0.0.1": _reply("127.0.0.1", "1756-L83E/B", 1)}, port)
        try:
            # 127.0.0.2 has no listener: it answers with ICMP port-unreachable, which on Windows used to
            # stop the socket receiving (regression test for _disable_udp_connreset).
            replies, errors = await discover(["127.0.0.2", "127.0.0.1"], port=port, timeout=0.3, rate=0)
            single = await probe("127.0.0.1", port=port, timeout=0.3)
        finally:
            for t in transports:
                t.close()
        return replies, errors, single

    replies, errors, single = asyncio.run(run())
    assert errors == []
    assert [r.source_ip for r in replies] == ["127.0.0.1"]
    assert replies[0].identity.product_name == "1756-L83E/B"
    assert single is not None and single.identity.revision == "33.011"


def test_lab_scan_without_switches_lists_every_live_host():
    """Replays a real lab scan: no managed switch, RSLinx PCs, a Moxa switch that only answers ARP."""
    from ghostmap.scanner import ScanRequest, run_scan

    port = _free_udp_port()
    moxa, stale = "00:90:e8:11:22:33", "00:11:22:33:44:55"
    linx_pc = lambda ctx: build_list_identity_response(  # noqa: E731
        ip="127.0.0.1", vendor_id=77, device_type=0x0C, product_code=115, revision=(16, 1), status=0x0060,
        serial=0x335F0A31, product_name="ENG-LAPTOP", state=255, context=ctx)

    async def run():
        transports = await serve({"127.0.0.1": linx_pc}, port)
        try:
            return await run_scan(
                ScanRequest(targets=["127.0.0.1-3"], enip_port=port, timeout=0.3, rate=0),
                arp_reader=lambda: {"127.0.0.2": moxa, "10.9.9.9": stale},
            )
        finally:
            for t in transports:
                t.close()

    scan = asyncio.run(run())
    by_ip = {d.ip: d for d in scan.devices}
    assert set(by_ip) == {"127.0.0.1", "127.0.0.2"}  # 10.9.9.9 was not swept, so it is ignored
    assert by_ip["127.0.0.2"].identity is None and by_ip["127.0.0.2"].mac_vendor.startswith("Moxa")
    pc = by_ip["127.0.0.1"]
    assert pc.is_scanner
    assert pc.identity.vendor_name == "Rockwell Software"
    assert pc.identity.device_type_name == "Workstation (RSLinx / FactoryTalk Linx)"
    assert not [f for f in scan.findings if f.code == "fw.mixed"]

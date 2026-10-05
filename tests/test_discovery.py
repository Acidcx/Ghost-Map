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
            replies, errors = await discover(["127.0.0.1", "127.0.0.2"], port=port, timeout=0.3, rate=0)
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

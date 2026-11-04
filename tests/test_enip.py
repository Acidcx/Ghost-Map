import struct

import pytest

from ghostmap.protocols import cip_tables
from ghostmap.protocols.enip import (
    CMD_LIST_IDENTITY,
    EnipError,
    build_list_identity,
    build_list_identity_response,
    parse_list_identity,
)


def test_request_is_24_byte_header():
    req = build_list_identity(b"abc")
    assert len(req) == 24
    command, length, session, status, ctx, opts = struct.unpack("<HHII8sI", req)
    assert (command, length, session, status, opts) == (CMD_LIST_IDENTITY, 0, 0, 0, 0)
    assert ctx == b"abc\0\0\0\0\0"


def test_roundtrip_identity():
    raw = build_list_identity_response(
        ip="192.168.1.10", vendor_id=1, device_type=0x0C, product_code=166, revision=(11, 2),
        status=0x0425, serial=0x00C0FFEE, product_name="1756-EN2T/D", state=4,
    )
    (ident,) = parse_list_identity(raw)
    assert ident.vendor_name == "Rockwell Automation/Allen-Bradley"
    assert ident.device_type_name == "Communications Adapter"
    assert ident.product_name == "1756-EN2T/D"
    assert ident.revision == "11.002"
    assert ident.serial_hex == "00C0FFEE"
    assert ident.socket_ip == "192.168.1.10"
    assert ident.socket_port == 44818
    assert ident.state_name == "Major Recoverable Fault"
    assert ident.extended_status == "At least one faulted I/O connection"
    assert set(ident.status_flags) == {"owned", "configured", "major_recoverable_fault"}


def test_unknown_vendor_and_type_are_labelled():
    assert cip_tables.vendor_name(65000) == "Vendor 65000"
    assert cip_tables.device_type_name(0x99) == "Device type 0x99"


def test_rejects_wrong_command_and_status():
    raw = bytearray(build_list_identity_response(
        ip="10.0.0.1", vendor_id=1, device_type=0, product_code=1, revision=(1, 1), status=0, serial=1,
        product_name="x"))
    bad_cmd = bytes([0x65, 0x00]) + bytes(raw[2:])
    with pytest.raises(EnipError):
        parse_list_identity(bad_cmd)
    raw[8] = 1  # encapsulation status
    with pytest.raises(EnipError):
        parse_list_identity(bytes(raw))


def test_truncated_reply_raises():
    raw = build_list_identity_response(
        ip="10.0.0.1", vendor_id=1, device_type=0, product_code=1, revision=(1, 1), status=0, serial=1,
        product_name="name")
    # keep header length field consistent with truncated payload
    payload = raw[24:24 + 20]
    hdr = struct.pack("<HHII8sI", CMD_LIST_IDENTITY, len(payload), 0, 0, b"\0" * 8, 0)
    with pytest.raises(EnipError):
        parse_list_identity(hdr + payload)


def test_short_header():
    with pytest.raises(EnipError):
        parse_list_identity(b"\x63\x00")


def test_rockwell_devices_seen_on_the_first_machine():
    """Replays what the first real machine (2026-11) sent: Kinetix 5700 drives and power supplies report device
    types that aren't in the ODVA list, and one Rockwell family sends a single letter as its product name."""
    def ident(**kw):
        base = dict(ip="10.10.0.5", vendor_id=1, revision=(14, 3), status=0x0060, serial=0x13E27427, state=3)
        (i,) = parse_list_identity(build_list_identity_response(**{**base, **kw}))
        return i

    assert ident(device_type=0x2D, product_code=10, product_name="2198-D006-ERS3").device_type_name == "Servo Drive (Kinetix 5700)"
    assert ident(device_type=0x30, product_code=1, product_name="2198-P031").device_type_name == "Servo Power Supply (Kinetix 5700)"
    p = ident(device_type=0x8F, product_code=2192, product_name="P", revision=(20, 4))
    assert p.product_name == 'Product code 2192 (sends "P" as its name)' and p.device_type_name == "Device type 0x8F"
    # Once the catalog number is known it is shown instead.
    cip_tables.PRODUCT_NAMES[(1, 0x8F, 2192)] = "Example product"
    try:
        assert ident(device_type=0x8F, product_code=2192, product_name="P").product_name == "Example product"
    finally:
        cip_tables.PRODUCT_NAMES.pop((1, 0x8F, 2192))
    assert cip_tables.device_type_name(0x2D, vendor_id=99, product_name="2198-D006") == "Device type 0x2D"


def test_identity_item_shorter_than_its_name():
    """A device that states an item length too short for its product name still gets its whole name read."""
    raw = bytearray(build_list_identity_response(ip="10.10.0.5", vendor_id=1, device_type=0x02, product_code=5,
                                                 revision=(1, 1), status=0, serial=1, product_name="PowerFlex Test"))
    item_len_at = 24 + 2 + 2  # header, item count, item type
    struct.pack_into("<H", raw, item_len_at, struct.unpack_from("<H", raw, item_len_at)[0] - 14)
    (i,) = parse_list_identity(bytes(raw))
    assert i.product_name == "PowerFlex Test"

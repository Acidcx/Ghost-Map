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

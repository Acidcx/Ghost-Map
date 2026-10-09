"""Minimal EtherNet/IP encapsulation: ListIdentity request/response.

ListIdentity (command 0x0063) is the same unconnected, read-only query that
RSLinx/FactoryTalk Linx "EtherNet/IP driver" browsing uses. It needs no
session, causes no I/O connection and changes nothing on the target.
"""

from __future__ import annotations

import socket
import struct

from ghostmap.models import CipIdentity
from ghostmap.protocols import cip_tables

ENIP_PORT = 44818
CMD_LIST_IDENTITY = 0x0063
ITEM_CIP_IDENTITY = 0x000C

_HEADER = struct.Struct("<HHII8sI")  # command, length, session, status, context, options
_SOCKADDR = struct.Struct(">hHI8s")  # sin_family, sin_port, sin_addr, sin_zero (big endian)
_IDENTITY_FIXED = struct.Struct("<HHHBBHI")  # vendor, type, product code, rev maj, rev min, status, serial


class EnipError(ValueError):
    pass


def build_list_identity(context: bytes = b"ghostmap") -> bytes:
    return _HEADER.pack(CMD_LIST_IDENTITY, 0, 0, 0, context[:8].ljust(8, b"\0"), 0)


def parse_list_identity(data: bytes) -> list[CipIdentity]:
    """Parse a ListIdentity reply into zero or more identities."""
    if len(data) < _HEADER.size:
        raise EnipError("short encapsulation header")
    command, length, _session, status, _ctx, _opts = _HEADER.unpack_from(data)
    if command != CMD_LIST_IDENTITY:
        raise EnipError(f"unexpected command 0x{command:04X}")
    if status != 0:
        raise EnipError(f"encapsulation status 0x{status:08X}")
    payload = data[_HEADER.size:_HEADER.size + length]
    if len(payload) < 2:
        return []

    (count,) = struct.unpack_from("<H", payload)
    offset = 2
    identities = []
    for _ in range(count):
        if offset + 4 > len(payload):
            raise EnipError("truncated item header")
        item_type, item_len = struct.unpack_from("<HH", payload, offset)
        offset += 4
        item = payload[offset:offset + item_len]
        offset += item_len
        if item_type == ITEM_CIP_IDENTITY:
            identities.append(_parse_identity_item(item))
    return identities


def _parse_identity_item(item: bytes) -> CipIdentity:
    try:
        (encap_version,) = struct.unpack_from("<H", item, 0)
        _family, port, addr, _zero = _SOCKADDR.unpack_from(item, 2)
        pos = 2 + _SOCKADDR.size
        vendor, dtype, pcode, rmaj, rmin, status, serial = _IDENTITY_FIXED.unpack_from(item, pos)
        pos += _IDENTITY_FIXED.size
        name_len = item[pos]
        name = item[pos + 1:pos + 1 + name_len].decode("latin-1", errors="replace")
        pos += 1 + name_len
        state = item[pos] if pos < len(item) else 255
    except (struct.error, IndexError) as exc:
        raise EnipError(f"malformed identity item: {exc}") from exc

    flags, extended = cip_tables.decode_status(status)
    return CipIdentity(
        vendor_id=vendor,
        vendor_name=cip_tables.vendor_name(vendor),
        device_type=dtype,
        device_type_name=cip_tables.device_type_name(dtype, vendor),
        product_code=pcode,
        revision_major=rmaj,
        revision_minor=rmin,
        status=status,
        serial=serial,
        product_name=name.strip(),
        state=state,
        state_name=cip_tables.state_name(state),
        status_flags=flags,
        extended_status=extended,
        encap_version=encap_version,
        socket_ip=socket.inet_ntoa(struct.pack(">I", addr)),
        socket_port=port,
    )


def build_list_identity_response(
    *,
    ip: str,
    vendor_id: int,
    device_type: int,
    product_code: int,
    revision: tuple[int, int],
    status: int,
    serial: int,
    product_name: str,
    state: int = 3,
    context: bytes = b"\0" * 8,
) -> bytes:
    """Build a ListIdentity reply. Used by the simulator and the tests."""
    name = product_name.encode("latin-1")[:255]
    item = (
        struct.pack("<H", 1)
        + _SOCKADDR.pack(socket.AF_INET, ENIP_PORT, struct.unpack(">I", socket.inet_aton(ip))[0], b"\0" * 8)
        + _IDENTITY_FIXED.pack(vendor_id, device_type, product_code, revision[0], revision[1], status, serial)
        + bytes([len(name)]) + name
        + bytes([state])
    )
    payload = struct.pack("<HHH", 1, ITEM_CIP_IDENTITY, len(item)) + item
    return _HEADER.pack(CMD_LIST_IDENTITY, len(payload), 0, 0, context[:8].ljust(8, b"\0"), 0) + payload

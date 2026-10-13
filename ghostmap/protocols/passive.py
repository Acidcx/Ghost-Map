"""Wire formats of what the passive listeners hear: BOOTP/DHCP requests, syslog lines and SNMP traps.

Only decoders, plus the matching encoders the simulator and tests use to make realistic packets. Nothing here
answers a packet: Ghost Map never acts as a BOOTP/DHCP server and never acknowledges an SNMP inform.
"""

from __future__ import annotations

import re
import struct
from typing import Any, Optional

BOOTP_MIN = 236
DHCP_COOKIE = b"\x63\x82\x53\x63"
DHCP_TYPES = {1: "DISCOVER", 2: "OFFER", 3: "REQUEST", 4: "DECLINE", 5: "ACK", 6: "NAK", 7: "RELEASE", 8: "INFORM"}


class PassiveError(ValueError):
    pass


def _ip(b: bytes) -> str:
    return ".".join(str(x) for x in b)


# ------------------------------------------------------------------ BOOTP / DHCP (UDP 67)
def parse_bootp(data: bytes) -> dict:
    """A BOOTP or DHCP message: {op, request, mac, xid, ciaddr, dhcp_type, hostname, vendor_class, requested_ip}."""
    if len(data) < BOOTP_MIN:
        raise PassiveError("short BOOTP message")
    op, htype, hlen = data[0], data[1], data[2]
    xid = struct.unpack_from(">I", data, 4)[0]
    mac = ":".join(f"{b:02x}" for b in data[28:28 + min(hlen, 16)]) if htype == 1 and hlen == 6 else ""
    out: dict[str, Any] = {"op": op, "request": op == 1, "mac": mac, "xid": xid, "ciaddr": _ip(data[12:16]),
                           "dhcp_type": None, "hostname": "", "vendor_class": "", "requested_ip": ""}
    if data[236:240] == DHCP_COOKIE:
        i = 240
        while i < len(data):
            code = data[i]
            if code == 255:
                break
            if code == 0:
                i += 1
                continue
            if i + 1 >= len(data):
                break
            n = data[i + 1]
            val = data[i + 2:i + 2 + n]
            i += 2 + n
            if code == 53 and val:
                out["dhcp_type"] = DHCP_TYPES.get(val[0], str(val[0]))
            elif code == 12:
                out["hostname"] = val.decode("latin-1", "replace").strip("\0")
            elif code == 60:
                out["vendor_class"] = val.decode("latin-1", "replace").strip("\0")
            elif code == 50 and len(val) == 4:
                out["requested_ip"] = _ip(val)
    return out


def build_bootp_request(mac: str, xid: int = 0x1234, dhcp: bool = False, hostname: str = "", vendor_class: str = "") -> bytes:
    hw = bytes(int(p, 16) for p in mac.split(":"))
    msg = struct.pack(">BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, xid, 0, 0x8000, bytes(4), bytes(4), bytes(4), bytes(4),
                      hw.ljust(16, b"\0"), b"", b"")
    if not dhcp:
        return msg + bytes(64)  # plain BOOTP: a vendor area, no options (what Rockwell modules send)
    opts = b"\x35\x01\x01"
    if hostname:
        opts += bytes([12, len(hostname)]) + hostname.encode()
    if vendor_class:
        opts += bytes([60, len(vendor_class)]) + vendor_class.encode()
    return msg + DHCP_COOKIE + opts + b"\xff"


# ------------------------------------------------------------------ syslog (UDP 514)
SYSLOG_SEVERITY = ["emergency", "alert", "critical", "error", "warning", "notice", "info", "debug"]
_PRI = re.compile(r"^<(\d{1,3})>")
_CISCO = re.compile(r"%([A-Z0-9_]+)-(\d)-([A-Z0-9_]+):\s*(.*)$", re.S)


def parse_syslog(data: bytes) -> dict:
    """A syslog line (RFC 3164, as IOS / Stratix send it): {severity, facility, tag, text}. Cisco-style messages
    also give {mnemonic: "LINK-3-UPDOWN", message}."""
    text = data.decode("utf-8", "replace").strip("\0\r\n ")
    if not text:
        raise PassiveError("empty syslog message")
    sev, fac = 6, 1
    m = _PRI.match(text)
    if m:
        pri = int(m.group(1))
        sev, fac = pri & 7, pri >> 3
        text = text[m.end():]
    out = {"severity": SYSLOG_SEVERITY[sev], "severity_n": sev, "facility": fac, "text": text[:1000], "mnemonic": "",
           "message": text[:1000]}
    c = _CISCO.search(text)
    if c:
        out.update(mnemonic=f"{c.group(1)}-{c.group(2)}-{c.group(3)}", message=c.group(4).strip()[:1000])
    return out


def build_syslog(text: str, severity: int = 5, facility: int = 23) -> bytes:
    return f"<{facility * 8 + severity}>{text}".encode()


# ------------------------------------------------------------------ SNMP traps (UDP 162), BER
SNMP_TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"
SYS_UPTIME_0 = "1.3.6.1.2.1.1.3.0"
GENERIC_TRAPS = ["coldStart", "warmStart", "linkDown", "linkUp", "authenticationFailure", "egpNeighborLoss", "enterpriseSpecific"]
V2_TRAPS = {f"1.3.6.1.6.3.1.1.5.{i + 1}": n for i, n in enumerate(GENERIC_TRAPS[:6])}
_APP = {0x40: "ip", 0x41: "counter", 0x42: "gauge", 0x43: "ticks", 0x44: "opaque", 0x46: "counter64"}


def _tlv(data: bytes, i: int) -> tuple[int, bytes, int]:
    if i + 2 > len(data):
        raise PassiveError("truncated BER")
    tag, n = data[i], data[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or i + k > len(data):
            raise PassiveError("bad BER length")
        n = int.from_bytes(data[i:i + k], "big")
        i += k
    if i + n > len(data):
        raise PassiveError("truncated BER value")
    return tag, data[i:i + n], i + n


def _seq(body: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(body):
        tag, val, i = _tlv(body, i)
        out.append((tag, val))
    return out


def _oid(b: bytes) -> str:
    if not b:
        return ""
    parts, v = [b[0] // 40, b[0] % 40], 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def _int(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=True) if b else 0


def _value(tag: int, b: bytes) -> Any:
    if tag == 0x02:
        return _int(b)
    if tag == 0x04:
        try:
            s = b.decode("utf-8")
            return s if s.isprintable() else b.hex(":")
        except UnicodeDecodeError:
            return b.hex(":")
    if tag == 0x06:
        return _oid(b)
    if tag == 0x40 and len(b) == 4:
        return _ip(b)
    if tag in _APP:
        return int.from_bytes(b, "big")
    return None


def parse_snmp_trap(data: bytes) -> dict:
    """An SNMP v1 trap, v2c trap or inform: {version, kind, trap, enterprise, agent, uptime_s, varbinds: [(oid, value)]}.
    The community is not returned (it is a password). SNMPv3 is reported as encrypted/not decoded."""
    tag, body, _ = _tlv(data, 0)
    if tag != 0x30:
        raise PassiveError("not an SNMP message")
    items = _seq(body)
    if len(items) < 3 or items[0][0] != 0x02:
        raise PassiveError("not an SNMP message")
    ver = _int(items[0][1])
    if ver == 3:
        return {"version": "v3", "kind": "v3", "trap": "", "enterprise": "", "agent": "", "uptime_s": None, "varbinds": []}
    pdu_tag, pdu = items[2]
    f = _seq(pdu)
    out: dict[str, Any] = {"version": "v1" if ver == 0 else "v2c", "kind": {0xA4: "trap", 0xA7: "trap", 0xA6: "inform"}.get(pdu_tag, ""),
                           "trap": "", "enterprise": "", "agent": "", "uptime_s": None, "varbinds": []}
    if not out["kind"]:
        raise PassiveError(f"not a trap (PDU 0x{pdu_tag:02X})")
    if pdu_tag == 0xA4:
        if len(f) < 6:
            raise PassiveError("short v1 trap")
        generic, specific = _int(f[2][1]), _int(f[3][1])
        out.update(enterprise=_oid(f[0][1]), agent=_value(f[1][0], f[1][1]) or "", uptime_s=_int(f[4][1]) // 100,
                   trap=GENERIC_TRAPS[generic] if 0 <= generic < 6 else f"{_oid(f[0][1])}.0.{specific}")
        vbs = f[5][1]
    else:
        if len(f) < 4:
            raise PassiveError("short v2 trap")
        vbs = f[3][1]
    for t, vb in _seq(vbs):
        pair = _seq(vb)
        if t == 0x30 and len(pair) == 2 and pair[0][0] == 0x06:
            out["varbinds"].append((_oid(pair[0][1]), _value(*pair[1])))
    if pdu_tag != 0xA4:
        vals = dict(out["varbinds"])
        trap_oid = str(vals.get(SNMP_TRAP_OID) or "")
        out["trap"] = V2_TRAPS.get(trap_oid, trap_oid)
        out["enterprise"] = trap_oid
        if isinstance(vals.get(SYS_UPTIME_0), int):
            out["uptime_s"] = vals[SYS_UPTIME_0] // 100
        out["varbinds"] = [v for v in out["varbinds"] if v[0] not in (SNMP_TRAP_OID, SYS_UPTIME_0)]
    return out


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _enc(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def _enc_int(v: int, tag: int = 0x02) -> bytes:
    n = max(1, (v.bit_length() + 8) // 8)
    return _enc(tag, v.to_bytes(n, "big", signed=True))


def _enc_oid(oid: str) -> bytes:
    p = [int(x) for x in oid.split(".")]
    body = bytes([p[0] * 40 + p[1]])
    for v in p[2:]:
        chunk = [v & 0x7F]
        v >>= 7
        while v:
            chunk.append(0x80 | (v & 0x7F))
            v >>= 7
        body += bytes(reversed(chunk))
    return _enc(0x06, body)


def _enc_val(v: Any) -> bytes:
    if isinstance(v, int):
        return _enc_int(v)
    if isinstance(v, tuple) and v[0] == "ticks":
        return _enc(0x43, v[1].to_bytes(max(1, (v[1].bit_length() + 7) // 8), "big"))
    if isinstance(v, tuple) and v[0] == "oid":
        return _enc_oid(v[1])
    return _enc(0x04, str(v).encode())


def build_trap_v2c(trap_oid: str, varbinds: list[tuple[str, Any]], community: str = "public", uptime_ticks: int = 100,
                   request_id: int = 1) -> bytes:
    vbs = [(SYS_UPTIME_0, ("ticks", uptime_ticks)), (SNMP_TRAP_OID, ("oid", trap_oid)), *varbinds]
    vb = b"".join(_enc(0x30, _enc_oid(o) + _enc_val(v)) for o, v in vbs)
    pdu = _enc(0xA7, _enc_int(request_id) + _enc_int(0) + _enc_int(0) + _enc(0x30, vb))
    return _enc(0x30, _enc_int(1) + _enc(0x04, community.encode()) + pdu)


def build_trap_v1(enterprise: str, agent: str, generic: int, specific: int = 0, varbinds: Optional[list] = None,
                  community: str = "public", uptime_ticks: int = 100) -> bytes:
    vb = b"".join(_enc(0x30, _enc_oid(o) + _enc_val(v)) for o, v in varbinds or [])
    pdu = _enc(0xA4, _enc_oid(enterprise) + _enc(0x40, bytes(int(x) for x in agent.split("."))) + _enc_int(generic)
               + _enc_int(specific) + _enc(0x43, uptime_ticks.to_bytes(4, "big")) + _enc(0x30, vb))
    return _enc(0x30, _enc_int(0) + _enc(0x04, community.encode()) + pdu)

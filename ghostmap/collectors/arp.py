"""Read the scanning host's own ARP cache.

After a unicast ListIdentity sweep of the local subnet the OS has ARP'd every
live host, so its cache gives IP -> MAC for devices on the same L2 segment,
even when the switch is an L2-only Stratix without a useful ARP table.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# ARP entry types from the Windows IP helper API (MIB_IPNET_TYPE): 2 is "invalid", the rest (other/dynamic/static)
# are real neighbours.
_ARP_INVALID = 2
_SKIP_MACS = ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00")

_MAC_RE = re.compile(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}")
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def normalize_mac(mac: str | bytes) -> str:
    if isinstance(mac, (bytes, bytearray)):
        return ":".join(f"{b:02x}" for b in mac)
    parts = re.split(r"[:\-.]", mac.strip())
    if len(parts) == 3:  # Cisco dotted 0011.2233.4455
        joined = "".join(parts)
        parts = [joined[i:i + 2] for i in range(0, 12, 2)]
    return ":".join(p.zfill(2) for p in parts).lower()


def parse_proc_net_arp(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 4 and cols[2] != "0x0" and cols[3] != "00:00:00:00:00:00":
            out[cols[0]] = normalize_mac(cols[3])
    return out


def parse_arp_a(text: str) -> dict[str, str]:
    """Parse ``arp -a`` output from Windows or macOS/BSD."""
    out = {}
    for line in text.splitlines():
        ip_m = _IP_RE.search(line)
        mac_m = _MAC_RE.search(line)
        if not ip_m or not mac_m:
            continue
        mac = normalize_mac(mac_m.group(0))
        if mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00") or mac.startswith("01:00:5e"):
            continue
        out[ip_m.group(1)] = mac
    return out


def _decode_ipnet_rows(rows: "list[tuple[int, bytes, int]]") -> dict[str, str]:
    """Turn raw ``(ipv4_as_little_endian_dword, mac_bytes, type)`` rows from the Windows ARP table into
    ``{ip: mac}``. Kept pure (no ctypes) so it can be tested off Windows."""
    out = {}
    for addr, mac, typ in rows:
        if typ == _ARP_INVALID or len(mac) != 6:
            continue
        m = normalize_mac(mac)
        if m in _SKIP_MACS or m.startswith("01:00:5e"):  # broadcast, empty, IPv4 multicast
            continue
        out[socket.inet_ntoa(struct.pack("<I", addr))] = m  # dwAddr holds the 4 address bytes in network order
    return out


def read_arp_windows() -> dict[str, str]:  # pragma: no cover - Windows only
    """Read this PC's ARP table through the IP helper API (``GetIpNetTable``), with no child process: ``arp.exe``
    was spawned every poll, which flashed a console and, as a repeated process launch by a service, looked like
    malware to Windows Defender's behaviour heuristics."""
    import ctypes
    from ctypes import wintypes

    class MIB_IPNETROW(ctypes.Structure):
        _fields_ = [("dwIndex", wintypes.DWORD), ("dwPhysAddrLen", wintypes.DWORD),
                    ("bPhysAddr", ctypes.c_ubyte * 8), ("dwAddr", wintypes.DWORD), ("dwType", wintypes.DWORD)]

    iphlpapi = ctypes.WinDLL("iphlpapi")
    size = wintypes.ULONG(0)
    iphlpapi.GetIpNetTable(None, ctypes.byref(size), False)  # first call just sizes the buffer
    if not size.value:
        return {}
    buf = (ctypes.c_byte * size.value)()
    if iphlpapi.GetIpNetTable(buf, ctypes.byref(size), False) != 0:  # 0 == NO_ERROR
        return {}
    num = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    table = ctypes.cast(ctypes.byref(buf, ctypes.sizeof(wintypes.DWORD)), ctypes.POINTER(MIB_IPNETROW * num)).contents
    rows = [(r.dwAddr, bytes(r.bPhysAddr[:r.dwPhysAddrLen]), r.dwType) for r in table]
    return _decode_ipnet_rows(rows)


def read_arp_cache() -> dict[str, str]:
    proc = Path("/proc/net/arp")
    if proc.is_file():
        return parse_proc_net_arp(proc.read_text())
    if sys.platform.startswith("win"):
        try:
            return read_arp_windows()
        except Exception:  # never spawn arp.exe as a fallback: a failed read is better than the behaviour we removed
            log.debug("reading the Windows ARP table failed", exc_info=True)
            return {}
    try:  # macOS / BSD: no /proc and no IP helper API
        text = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_arp_a(text)

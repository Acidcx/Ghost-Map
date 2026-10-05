"""Read the scanning host's own ARP cache.

After a unicast ListIdentity sweep of the local subnet the OS has ARP'd every
live host, so its cache gives IP -> MAC for devices on the same L2 segment,
even when the switch is an L2-only Stratix without a useful ARP table.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

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


def read_arp_cache() -> dict[str, str]:
    proc = Path("/proc/net/arp")
    if proc.is_file():
        return parse_proc_net_arp(proc.read_text())
    try:
        args = ["arp", "-a"] if sys.platform.startswith("win") else ["arp", "-an"]
        text = subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_arp_a(text)

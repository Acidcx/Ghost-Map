"""Rates from two readings of a switch's port counters: bandwidth, broadcast and multicast packets,
errors and drops per second. Pure functions; the readings come from ``collectors/portstats.py``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ghostmap.models import PortTraffic

# Counter name -> (PortTraffic field, multiplier). Octets become bits.
RATES = {
    "in_octets": ("in_bps", 8), "out_octets": ("out_bps", 8),
    "in_bcast": ("in_bcast_pps", 1), "out_bcast": ("out_bcast_pps", 1),
    "in_mcast": ("in_mcast_pps", 1), "out_mcast": ("out_mcast_pps", 1),
    "in_errors": ("in_errors_ps", 1), "in_discards": ("in_discards_ps", 1), "out_discards": ("out_discards_ps", 1),
}


@dataclass
class CounterSample:
    """One reading of every port's counters. ``bits`` is the counter width per counter name (64 or 32)."""

    at: float  # monotonic seconds
    ports: dict[int, dict[str, int]] = field(default_factory=dict)
    bits: dict[str, int] = field(default_factory=dict)
    speed_mbps: dict[int, int] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)
    sys_uptime: Optional[int] = None  # ticks; going backwards means the switch restarted


def _delta(a: Optional[int], b: Optional[int], bits: int) -> Optional[int]:
    if a is None or b is None:
        return None
    d = b - a
    if d < 0:
        if bits != 32:
            return None  # a 64-bit counter going backwards was cleared or the switch restarted
        d += 1 << 32  # 32-bit counters wrap every ~6 minutes at 100 Mbps; assume one wrap
    return d


def traffic_between(prev: CounterSample, cur: CounterSample) -> dict[int, PortTraffic]:
    """Rates per ifIndex. Empty when the two readings can't be compared (restart, no time between them)."""
    seconds = cur.at - prev.at
    if seconds <= 0:
        return {}
    if prev.sys_uptime is not None and cur.sys_uptime is not None and cur.sys_uptime < prev.sys_uptime:
        return {}
    out: dict[int, PortTraffic] = {}
    for idx, now in cur.ports.items():
        before = prev.ports.get(idx)
        if before is None:
            continue
        t = PortTraffic(seconds=round(seconds, 1))
        for name, (attr, mult) in RATES.items():
            d = _delta(before.get(name), now.get(name), cur.bits.get(name, 64))
            if d is not None:
                setattr(t, attr, round(d * mult / seconds, 1))
        speed = cur.speed_mbps.get(idx) or 0
        if speed > 0:
            if t.in_bps is not None:
                t.in_util = round(min(100.0, 100.0 * t.in_bps / (speed * 1e6)), 1)
            if t.out_bps is not None:
                t.out_util = round(min(100.0, 100.0 * t.out_bps / (speed * 1e6)), 1)
        out[idx] = t
    return out

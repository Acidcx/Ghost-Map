"""Read a switch's per-port traffic counters over read-only SNMP (IF-MIB), for bandwidth and storm rates.

A rate needs two readings: ``read_counters`` takes one; ``analysis/traffic.py`` turns two into rates.
Scans take two readings around the rest of the switch collection (``sample_traffic``); the background
monitor (``ghostmap/netmon.py``) reads every poll interval.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from ghostmap.analysis.traffic import CounterSample, traffic_between
from ghostmap.models import SwitchInfo
from ghostmap.protocols import mibs
from ghostmap.protocols.snmp import SnmpClient, column

log = logging.getLogger(__name__)

# Counter name -> (64-bit column, 32-bit fallback or None)
COUNTERS = {
    "in_octets": (mibs.IF_HC_IN_OCTETS, mibs.IF_IN_OCTETS),
    "out_octets": (mibs.IF_HC_OUT_OCTETS, mibs.IF_OUT_OCTETS),
    "in_bcast": (mibs.IF_HC_IN_BCAST, mibs.IF_IN_BCAST),
    "out_bcast": (mibs.IF_HC_OUT_BCAST, mibs.IF_OUT_BCAST),
    "in_mcast": (mibs.IF_HC_IN_MCAST, mibs.IF_IN_MCAST),
    "out_mcast": (mibs.IF_HC_OUT_MCAST, mibs.IF_OUT_MCAST),
    "in_errors": (mibs.IF_IN_ERRORS, None),
    "in_discards": (mibs.IF_IN_DISCARDS, None),
    "out_discards": (mibs.IF_OUT_DISCARDS, None),
}


def _int(v: Any):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def read_counters(client: SnmpClient, clock: Callable[[], float] = time.monotonic,
                        with_names: bool = False) -> CounterSample:
    """One reading of every port's counters, plus link speeds (and names, for the monitor)."""
    uptime = (await client.get([mibs.SYS_UPTIME])).get(mibs.SYS_UPTIME)
    sample = CounterSample(at=clock(), sys_uptime=_int(uptime))
    for name, (hc, legacy) in COUNTERS.items():
        rows = column(await client.walk(hc), hc)
        sample.bits[name] = 64 if hc.startswith("1.3.6.1.2.1.31.") else 32
        if not rows and legacy:
            rows = column(await client.walk(legacy), legacy)
            sample.bits[name] = 32
        for idx, v in rows.items():
            n = _int(v)
            if n is not None and idx.isdigit():
                sample.ports.setdefault(int(idx), {})[name] = n
    for idx, v in column(await client.walk(mibs.IF_HIGH_SPEED), mibs.IF_HIGH_SPEED).items():
        if idx.isdigit() and _int(v):
            sample.speed_mbps[int(idx)] = _int(v)
    if with_names:
        for idx, v in column(await client.walk(mibs.IF_NAME), mibs.IF_NAME).items():
            if idx.isdigit():
                sample.names[int(idx)] = v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
    sample.at = (sample.at + clock()) / 2  # the reading took a moment; date it in the middle
    return sample


async def sample_traffic(ip: str, client: SnmpClient, collect: Callable[[], Awaitable[SwitchInfo]], window: float,
                         sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                         clock: Callable[[], float] = time.monotonic) -> SwitchInfo:
    """Read the counters, run ``collect`` (the rest of the switch reading), wait until ``window`` seconds have
    passed since the first reading, read again and put the rates on the ports. ``window <= 0``: no rates."""
    if window <= 0:
        return await collect()
    try:
        first = await read_counters(client, clock)
    except Exception as exc:
        log.debug("switch %s first traffic reading failed", ip, exc_info=True)
        sw = await collect()
        sw.errors.append(f"traffic: {exc}")
        return sw
    sw = await collect()
    if any(e.startswith("system:") for e in sw.errors):
        return sw
    try:
        wait = window - (clock() - first.at)
        if wait > 0:
            await sleep(wait)
        rates = traffic_between(first, await read_counters(client, clock))
    except Exception as exc:
        log.debug("switch %s second traffic reading failed", ip, exc_info=True)
        sw.errors.append(f"traffic: {exc}")
        return sw
    for p in sw.ports:
        p.traffic = rates.get(p.if_index)
    return sw

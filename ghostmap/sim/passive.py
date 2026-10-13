"""What the passive listeners would hear on the demo machine (``ghostmap web --demo``).

Made-up packets, encoded with the real wire formats and fed through the same decoders: the drive on Fa1/2 with a
bad cable flaps its port (syslog and traps from the switch), a factory-reset I/O adapter keeps asking for an IP
over BOOTP, a laptop appears, and later a second device takes the laptop's IP.
"""

from __future__ import annotations

import asyncio
import time

from ghostmap.protocols.passive import build_bootp_request, build_syslog, build_trap_v2c
from ghostmap.sim.machine import SWITCH_IP, demo_machine

RESET_MODULE_MAC = "00:00:bc:ee:00:60"  # a Rockwell OUI: an adapter fresh from the box, BOOTP on
LAPTOP = ("192.168.1.77", "00:0e:c6:12:34:77")
CONFLICT_MAC = "3c:52:82:11:22:33"
TICK_S = 2.0
LINK_DOWN, LINK_UP = "1.3.6.1.6.3.1.1.5.3", "1.3.6.1.6.3.1.1.5.4"


def _link_syslog(port: str, up: bool) -> bytes:
    return build_syslog(f"1042: CELL1-SW01: %LINK-3-UPDOWN: Interface {port}, changed state to {'up' if up else 'down'}", severity=3)


def _link_trap(if_index: int, name: str, alias: str, up: bool) -> bytes:
    return build_trap_v2c(LINK_UP if up else LINK_DOWN, [
        (f"1.3.6.1.2.1.2.2.1.1.{if_index}", if_index), (f"1.3.6.1.2.1.31.1.1.1.1.{if_index}", name),
        (f"1.3.6.1.2.1.31.1.1.1.18.{if_index}", alias)], uptime_ticks=360000 * 24 * 12)


def arp_table(extra: dict | None = None) -> dict:
    devices, _ = demo_machine("today")
    return {d.ip: d.mac for d in devices if d.ip} | (extra or {})


def backfill(mon, now: float) -> None:
    """The last ten minutes, so the page has something to show at once."""
    base = arp_table()
    mon.arp_update({k: v for k, v in base.items() if k != "192.168.1.50"}, at=now - 600)
    mon.feed("syslog", _link_syslog("FastEthernet1/8", False), SWITCH_IP, at=now - 540)
    mon.feed("traps", _link_trap(9, "Fa1/8", "ROBOT", False), SWITCH_IP, at=now - 539)
    mon.feed("syslog", build_syslog("1050: CELL1-SW01: %CDP-4-DUPLEX_MISMATCH: duplex mismatch discovered on "
                                    "FastEthernet1/2 (not half duplex), with VFD-MAIN-CONV (half duplex).", severity=4),
             SWITCH_IP, at=now - 420)
    mon.arp_update(base, at=now - 300)
    mon.feed("bootp", build_bootp_request(RESET_MODULE_MAC, xid=0x51), "0.0.0.0", at=now - 200)
    mon.feed("syslog", _link_syslog("FastEthernet1/2", False), SWITCH_IP, at=now - 95)
    mon.feed("traps", _link_trap(3, "Fa1/2", "VFD-MAIN-CONV", False), SWITCH_IP, at=now - 94)
    mon.feed("syslog", _link_syslog("FastEthernet1/2", True), SWITCH_IP, at=now - 91)
    mon.feed("traps", _link_trap(3, "Fa1/2", "VFD-MAIN-CONV", True), SWITCH_IP, at=now - 90)
    mon.arp_update(base, at=now - 10)


async def run(mon) -> None:
    t0 = time.time()
    backfill(mon, t0)
    step = 0
    while True:
        await asyncio.sleep(TICK_S)
        step += 1
        t = step * TICK_S
        cycle = t % 60
        if step % 5 == 0:  # the ARP table, every 10 s
            extra = {}
            if t >= 30:
                extra[LAPTOP[0]] = LAPTOP[1]
            if 120 <= t < 150:
                extra[LAPTOP[0]] = CONFLICT_MAC
            mon.arp_update(arp_table(extra))
        if cycle == 10:
            mon.feed("syslog", _link_syslog("FastEthernet1/2", False), SWITCH_IP)
            mon.feed("traps", _link_trap(3, "Fa1/2", "VFD-MAIN-CONV", False), SWITCH_IP)
        elif cycle == 14:
            mon.feed("syslog", _link_syslog("FastEthernet1/2", True), SWITCH_IP)
            mon.feed("traps", _link_trap(3, "Fa1/2", "VFD-MAIN-CONV", True), SWITCH_IP)
        elif cycle == 30:
            mon.feed("bootp", build_bootp_request(RESET_MODULE_MAC, xid=0x51 + step), "0.0.0.0")

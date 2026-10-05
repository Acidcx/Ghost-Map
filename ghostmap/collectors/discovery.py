"""EtherNet/IP device discovery via paced ListIdentity requests.

Two modes, usable together:
  * unicast sweep of one or more CIDRs (works across routers, finds devices
    that ignore broadcasts)
  * broadcast to a subnet broadcast address (fast, local segment only)

Traffic is rate limited (``rate`` packets/second) so the sweep is gentle on
small embedded stacks and on busy control networks.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Callable, Iterable, Optional

from ghostmap.models import DiscoveredIdentity
from ghostmap.protocols.enip import ENIP_PORT, EnipError, build_list_identity, parse_list_identity

log = logging.getLogger(__name__)

DEFAULT_MAX_HOSTS = 4096


def expand_targets(specs: Iterable[str], max_hosts: int = DEFAULT_MAX_HOSTS) -> list[str]:
    """Expand ``10.0.0.0/24``, ``10.0.0.5`` or ``10.0.0.10-10.0.0.20`` into host IPs."""
    hosts: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        if "-" in spec:
            start_s, end_s = (s.strip() for s in spec.split("-", 1))
            start = ipaddress.IPv4Address(start_s)
            end = ipaddress.IPv4Address(end_s) if "." in end_s else ipaddress.IPv4Address(
                f"{start_s.rsplit('.', 1)[0]}.{end_s}"
            )
            if int(end) < int(start):
                raise ValueError(f"bad range {spec!r}")
            count = int(end) - int(start) + 1
        else:
            net = ipaddress.IPv4Network(spec, strict=False)
            count = net.num_addresses
        if len(hosts) + count > max_hosts + 2:  # +2: network/broadcast are excluded below
            raise ValueError(f"target list exceeds {max_hosts} hosts; narrow the range or raise max_hosts")
        if "-" in spec:
            new = [str(ipaddress.IPv4Address(i)) for i in range(int(start), int(end) + 1)]
        else:
            new = [str(h) for h in net.hosts()] if net.num_addresses > 1 else [str(net.network_address)]
        for h in new:
            if h not in seen:
                seen.add(h)
                hosts.append(h)
        if len(hosts) > max_hosts:
            raise ValueError(f"target list exceeds {max_hosts} hosts; narrow the range or raise max_hosts")
    return hosts


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.results: list[DiscoveredIdentity] = []
        self.errors: list[str] = []

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            for ident in parse_list_identity(data):
                self.results.append(DiscoveredIdentity(source_ip=addr[0], identity=ident))
        except EnipError as exc:
            self.errors.append(f"{addr[0]}: {exc}")

    def error_received(self, exc: Exception) -> None:  # ICMP unreachable etc.
        log.debug("discovery socket error: %s", exc)


async def discover(
    targets: Iterable[str] = (),
    broadcast: Iterable[str] = (),
    *,
    port: int = ENIP_PORT,
    timeout: float = 2.0,
    rate: float = 200.0,
    bind: tuple[str, int] = ("0.0.0.0", 0),
    progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[list[DiscoveredIdentity], list[str]]:
    """Send ListIdentity to every target and collect replies.

    Returns ``(replies, errors)``. Replies are *not* de-duplicated so callers
    can detect duplicate IPs (two different identities answering from one IP).
    """
    targets = list(targets)
    broadcast = list(broadcast)
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(bind)
    transport, proto = await loop.create_datagram_endpoint(_Collector, sock=sock)
    request = build_list_identity()
    interval = 1.0 / rate if rate > 0 else 0.0
    try:
        for addr in broadcast:
            transport.sendto(request, (addr, port))
        total = len(targets)
        for i, ip in enumerate(targets, 1):
            transport.sendto(request, (ip, port))
            if progress and (i % 16 == 0 or i == total):
                progress(i, total)
            if interval:
                await asyncio.sleep(interval)
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return proto.results, proto.errors


async def probe(ip: str, *, port: int = ENIP_PORT, timeout: float = 1.5) -> Optional[DiscoveredIdentity]:
    """Single device ListIdentity (debug helper)."""
    results, _ = await discover([ip], port=port, timeout=timeout, rate=0)
    return results[0] if results else None


async def tcp_port_open(ip: str, port: int, timeout: float = 1.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True

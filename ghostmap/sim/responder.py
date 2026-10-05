"""EtherNet/IP ListIdentity responder for bench testing without hardware.

Binds one UDP socket per simulated device IP. On Linux every 127.x.y.z
address is loopback, so ``ghostmap simulate`` can stand up a whole machine on
127.0.10.0/24 without root and without touching a real network.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Callable

from ghostmap.protocols.enip import CMD_LIST_IDENTITY, ENIP_PORT

_HEADER = struct.Struct("<HHII8sI")


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, reply_for_context: Callable[[bytes], bytes]):
        self.reply_for_context = reply_for_context
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < _HEADER.size:
            return
        command, _len, _session, _status, context, _opts = _HEADER.unpack_from(data)
        if command == CMD_LIST_IDENTITY:
            self.transport.sendto(self.reply_for_context(context), addr)


async def serve(replies: dict[str, Callable[[bytes], bytes]], port: int = ENIP_PORT) -> list[asyncio.DatagramTransport]:
    """``replies`` maps bind IP -> function(context) -> reply bytes."""
    loop = asyncio.get_running_loop()
    transports = []
    for ip, fn in replies.items():
        transport, _ = await loop.create_datagram_endpoint(lambda fn=fn: _Responder(fn), local_addr=(ip, port))
        transports.append(transport)
    return transports

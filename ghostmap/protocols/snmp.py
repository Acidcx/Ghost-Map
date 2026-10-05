"""Read-only SNMP client abstraction.

Collectors only depend on :class:`SnmpClient` (``get`` and ``walk``), so the
switch logic can be tested and demoed against :class:`FakeSnmpClient` without
a network. :class:`PySnmpClient` is the real implementation (pysnmp 7).

There is deliberately no ``set`` method anywhere in Ghost Map.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any, Optional, Protocol

Varbind = tuple[str, Any]


@dataclass
class SnmpCredentials:
    version: str = "2c"  # "2c" | "3"
    community: str = "public"
    username: str = ""
    auth_protocol: str = "sha"  # none | md5 | sha | sha256
    auth_key: str = ""
    priv_protocol: str = "aes"  # none | des | aes | aes256
    priv_key: str = ""
    port: int = 161
    timeout: float = 2.0
    retries: int = 1

    def redacted(self) -> dict[str, Any]:
        """Safe to store in scan results / logs."""
        out: dict[str, Any] = {"version": self.version, "port": self.port}
        if self.version == "3":
            out.update(username=self.username, auth_protocol=self.auth_protocol, priv_protocol=self.priv_protocol)
        return out


class SnmpError(RuntimeError):
    pass


class SnmpClient(Protocol):
    async def get(self, oids: list[str]) -> dict[str, Any]: ...

    async def walk(self, oid: str, vlan: Optional[int] = None) -> list[Varbind]: ...

    async def close(self) -> None: ...


def oid_suffix(oid: str, base: str) -> Optional[str]:
    """Return the index part of ``oid`` below ``base`` or None if outside the subtree."""
    prefix = base.rstrip(".") + "."
    return oid[len(prefix):] if oid.startswith(prefix) else None


def column(rows: list[Varbind], base: str) -> dict[str, Any]:
    """Turn a walk result into ``{index: value}``."""
    out = {}
    for oid, value in rows:
        idx = oid_suffix(oid, base)
        if idx is not None:
            out[idx] = value
    return out


class FakeSnmpClient:
    """In-memory agent backed by an ``{oid: value}`` dict (per VLAN context optional)."""

    def __init__(self, data: dict[str, Any], vlan_data: Optional[dict[int, dict[str, Any]]] = None):
        self._data = data
        self._vlan_data = vlan_data or {}

    @staticmethod
    def _key(oid: str) -> tuple[int, ...]:
        return tuple(int(p) for p in oid.split("."))

    async def get(self, oids: list[str]) -> dict[str, Any]:
        return {oid: self._data.get(oid) for oid in oids}

    async def walk(self, oid: str, vlan: Optional[int] = None) -> list[Varbind]:
        data = self._vlan_data.get(vlan, {}) if vlan is not None else self._data
        return sorted(((k, v) for k, v in data.items() if oid_suffix(k, oid) is not None), key=lambda kv: self._key(kv[0]))

    async def close(self) -> None:
        return None


class PySnmpClient:
    """SNMP v2c / v3 client on top of pysnmp's asyncio high level API."""

    _AUTH = {"none": "usmNoAuthProtocol", "md5": "usmHMACMD5AuthProtocol", "sha": "usmHMACSHAAuthProtocol",
             "sha256": "usmHMAC192SHA256AuthProtocol"}
    _PRIV = {"none": "usmNoPrivProtocol", "des": "usmDESPrivProtocol", "aes": "usmAesCfb128Protocol",
             "aes256": "usmAesCfb256Protocol"}

    def __init__(self, host: str, creds: SnmpCredentials):
        from pysnmp.hlapi.v3arch import asyncio as hl

        self._hl = hl
        self.host = host
        self.creds = creds
        self._engine = hl.SnmpEngine()
        self._target = None

    async def _transport(self):
        if self._target is None:
            self._target = await self._hl.UdpTransportTarget.create(
                (self.host, self.creds.port), timeout=self.creds.timeout, retries=self.creds.retries
            )
        return self._target

    def _auth(self, vlan: Optional[int]):
        hl, c = self._hl, self.creds
        if c.version == "3":
            return hl.UsmUserData(
                c.username,
                authKey=c.auth_key or None,
                privKey=c.priv_key or None,
                authProtocol=getattr(hl, self._AUTH[c.auth_protocol]),
                privProtocol=getattr(hl, self._PRIV[c.priv_protocol]),
            )
        # Cisco IOS community string indexing for per-VLAN BRIDGE-MIB.
        community = f"{c.community}@{vlan}" if vlan is not None else c.community
        return hl.CommunityData(community, mpModel=1)

    def _context(self, vlan: Optional[int]):
        if self.creds.version == "3" and vlan is not None:
            return self._hl.ContextData(contextName=f"vlan-{vlan}".encode())
        return self._hl.ContextData()

    async def get(self, oids: list[str]) -> dict[str, Any]:
        hl = self._hl
        err, status, index, binds = await hl.get_cmd(
            self._engine, self._auth(None), await self._transport(), self._context(None),
            *[hl.ObjectType(hl.ObjectIdentity(o)) for o in oids],
        )
        if err:
            raise SnmpError(f"{self.host}: {err}")
        if status:
            raise SnmpError(f"{self.host}: {status.prettyPrint()} at {index}")
        return {str(name): _py(value) for name, value in binds}

    async def walk(self, oid: str, vlan: Optional[int] = None) -> list[Varbind]:
        hl = self._hl
        rows: list[Varbind] = []
        async for err, status, _index, binds in hl.bulk_walk_cmd(
            self._engine, self._auth(vlan), await self._transport(), self._context(vlan),
            0, 25, hl.ObjectType(hl.ObjectIdentity(oid)), lexicographicMode=False,
        ):
            if err:
                raise SnmpError(f"{self.host}: {err}")
            if status:
                raise SnmpError(f"{self.host}: {status.prettyPrint()}")
            for name, value in binds:
                py = _py(value)
                if py is not None:
                    rows.append((str(name), py))
        return rows

    async def close(self) -> None:
        try:
            self._engine.close_dispatcher()
        except Exception:  # pragma: no cover - best effort cleanup
            pass


def _py(value: Any) -> Any:
    """Convert pysnmp/pyasn1 values to plain Python."""
    cls = type(value).__name__
    if cls in ("NoSuchObject", "NoSuchInstance", "EndOfMibView", "Null"):
        return None
    if cls == "IpAddress":
        return socket.inet_ntoa(bytes(value.asOctets()))
    if cls == "ObjectIdentifier":
        return str(value)
    if hasattr(value, "asOctets"):
        return bytes(value.asOctets())
    try:
        return int(value)
    except (TypeError, ValueError):
        return value.prettyPrint() if hasattr(value, "prettyPrint") else value

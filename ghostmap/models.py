"""Core data model shared by collectors, analysis, storage and the web UI.

Everything is a plain dataclass so a scan can be written to / read from JSON
without extra dependencies.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Optional, Union


@dataclass
class CipIdentity:
    """CIP Identity object (class 0x01) as returned by EtherNet/IP ListIdentity."""

    vendor_id: int
    vendor_name: str
    device_type: int
    device_type_name: str
    product_code: int
    revision_major: int
    revision_minor: int
    status: int
    serial: int
    product_name: str
    state: int
    state_name: str = ""
    status_flags: list[str] = field(default_factory=list)
    extended_status: str = ""
    encap_version: int = 1
    socket_ip: str = ""
    socket_port: int = 0

    @property
    def revision(self) -> str:
        # Rockwell convention: minor revision is shown zero padded, e.g. 11.002
        return f"{self.revision_major}.{self.revision_minor:03d}"

    @property
    def serial_hex(self) -> str:
        return f"{self.serial:08X}"

    def summary(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor_name,
            "product": self.product_name,
            "type": self.device_type_name,
            "revision": self.revision,
            "serial": self.serial_hex,
        }


@dataclass
class DiscoveredIdentity:
    """One ListIdentity reply, exactly as received (kept for duplicate-IP checks)."""

    source_ip: str
    identity: CipIdentity


@dataclass
class Neighbor:
    protocol: str  # "lldp" | "cdp"
    local_port: str
    remote_name: str = ""
    remote_port: str = ""
    remote_platform: str = ""
    remote_address: str = ""


@dataclass
class Port:
    if_index: int
    name: str = ""
    descr: str = ""
    alias: str = ""
    if_type: int = 0
    admin_status: str = "unknown"  # up | down | testing | unknown
    oper_status: str = "unknown"
    speed_mbps: int = 0
    duplex: str = "unknown"  # full | half | unknown
    vlan: Optional[int] = None
    mac: str = ""
    last_change_ticks: int = 0
    in_errors: int = 0
    out_errors: int = 0
    in_discards: int = 0
    out_discards: int = 0
    fcs_errors: int = 0
    alignment_errors: int = 0
    late_collisions: int = 0
    macs: list[str] = field(default_factory=list)
    neighbors: list[Neighbor] = field(default_factory=list)
    is_uplink: bool = False

    @property
    def is_physical(self) -> bool:
        # ethernetCsmacd(6), gigabitEthernet(117)
        return self.if_type in (6, 117)


@dataclass
class SwitchInfo:
    ip: str
    sys_name: str = ""
    sys_descr: str = ""
    sys_object_id: str = ""
    uptime_seconds: int = 0
    location: str = ""
    contact: str = ""
    model: str = ""
    serial: str = ""
    software_version: str = ""
    hardware_revision: str = ""
    ports: list[Port] = field(default_factory=list)
    arp: dict[str, str] = field(default_factory=dict)  # ip -> mac
    errors: list[str] = field(default_factory=list)

    def port_by_name(self, name: str) -> Optional[Port]:
        for p in self.ports:
            if p.name == name:
                return p
        return None


@dataclass
class Device:
    """A correlated endpoint: CIP identity + MAC + switch location."""

    ip: Optional[str] = None
    mac: Optional[str] = None
    identity: Optional[CipIdentity] = None
    switch_ip: Optional[str] = None
    switch_name: Optional[str] = None
    switch_port: Optional[str] = None
    vlan: Optional[int] = None
    sources: list[str] = field(default_factory=list)
    mac_vendor: str = ""  # manufacturer from the MAC address (IEEE OUI)
    is_scanner: bool = False  # this is the computer running Ghost Map

    @property
    def key(self) -> str:
        """Stable identity used to track a device across scans."""
        if self.identity is not None:
            return f"cip:{self.identity.vendor_id}:{self.identity.serial_hex}"
        if self.mac:
            return f"mac:{self.mac}"
        return f"ip:{self.ip}"

    @property
    def label(self) -> str:
        if self.identity is not None:
            return f"{self.identity.product_name} @ {self.ip}"
        return self.ip or self.mac or "?"


SEVERITIES = ("error", "warning", "info")


@dataclass
class Finding:
    severity: str  # error | warning | info
    code: str
    target: str
    message: str
    hint: str = ""


@dataclass
class ScanResult:
    id: str
    started: str
    finished: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    identities: list[DiscoveredIdentity] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    switches: list[SwitchInfo] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# JSON (de)serialisation helpers
# --------------------------------------------------------------------------

def to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses to JSON friendly structures."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        # Expose a few computed properties so the UI does not have to re-derive them.
        if isinstance(obj, CipIdentity):
            out["revision"] = obj.revision
            out["serial_hex"] = obj.serial_hex
        elif isinstance(obj, Device):
            out["key"] = obj.key
        return out
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


def from_dict(cls: type, data: Any) -> Any:
    """Inverse of :func:`to_dict` driven by the dataclass type hints."""
    return _convert(cls, data)


def _convert(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return _convert(args[0], value) if len(args) == 1 else value
    if origin is list:
        (item_tp,) = typing.get_args(tp)
        return [_convert(item_tp, v) for v in value]
    if origin is dict:
        _, val_tp = typing.get_args(tp)
        return {k: _convert(val_tp, v) for k, v in value.items()}
    if dataclasses.is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        kwargs = {}
        for f in dataclasses.fields(tp):
            if f.name in value:
                kwargs[f.name] = _convert(hints[f.name], value[f.name])
        return tp(**kwargs)
    return value

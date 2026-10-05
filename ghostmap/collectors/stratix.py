"""Stratix (Cisco IOS based) switch collector over read-only SNMP.

Collects: system info, chassis model/serial/IOS version, per-port status,
speed, duplex, VLAN, error counters, MAC address table, ARP cache and
LLDP/CDP neighbours. Every section is best-effort: a failure is recorded in
``SwitchInfo.errors`` and collection continues.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from ghostmap.collectors.arp import normalize_mac
from ghostmap.models import Neighbor, Port, SwitchInfo
from ghostmap.protocols import mibs
from ghostmap.protocols.snmp import SnmpClient, column

log = logging.getLogger(__name__)

_IOS_VERSION_RE = re.compile(r"Version\s+([^\s,]+)")
# VLANs that never carry endpoint MACs on IOS (default FDDI/token ring VLANs)
_RESERVED_VLANS = {1002, 1003, 1004, 1005}


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace").strip("\x00").strip()
    return "" if value is None else str(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mac_from_index(idx: str) -> str:
    parts = [int(p) for p in idx.split(".")[-6:]]
    return ":".join(f"{p:02x}" for p in parts)


async def collect_switch(ip: str, client: SnmpClient) -> SwitchInfo:
    sw = SwitchInfo(ip=ip)

    async def section(name: str, fn: Callable[[], Awaitable[None]]) -> None:
        try:
            await fn()
        except Exception as exc:  # keep going, record what failed
            log.debug("switch %s section %s failed", ip, name, exc_info=True)
            sw.errors.append(f"{name}: {exc}")

    await section("system", lambda: _system(sw, client))
    if any(e.startswith("system:") for e in sw.errors):
        return sw  # unreachable or bad credentials; no point continuing
    await section("entity", lambda: _entity(sw, client))
    await section("interfaces", lambda: _interfaces(sw, client))
    ports = {p.if_index: p for p in sw.ports}
    await section("ethernet", lambda: _ethernet(ports, client))
    await section("vlans", lambda: _port_vlans(ports, client))
    await section("mac-table", lambda: _mac_table(ports, client))
    await section("arp", lambda: _arp(sw, client))
    await section("lldp", lambda: _lldp(sw, ports, client))
    await section("cdp", lambda: _cdp(ports, client))
    _mark_uplinks(sw)
    return sw


async def _system(sw: SwitchInfo, client: SnmpClient) -> None:
    vals = await client.get(list(mibs.SYSTEM_SCALARS))
    if all(v is None for v in vals.values()):
        raise RuntimeError("no response to system group")
    sw.sys_descr = _text(vals.get(mibs.SYS_DESCR))
    sw.sys_object_id = _text(vals.get(mibs.SYS_OBJECT_ID))
    sw.uptime_seconds = _int(vals.get(mibs.SYS_UPTIME)) // 100
    sw.contact = _text(vals.get(mibs.SYS_CONTACT))
    sw.sys_name = _text(vals.get(mibs.SYS_NAME))
    sw.location = _text(vals.get(mibs.SYS_LOCATION))
    m = _IOS_VERSION_RE.search(sw.sys_descr)
    if m:
        sw.software_version = m.group(1)


async def _entity(sw: SwitchInfo, client: SnmpClient) -> None:
    classes = column(await client.walk(mibs.ENT_CLASS), mibs.ENT_CLASS)
    chassis = [idx for idx, cls in classes.items() if _int(cls) == mibs.ENT_CLASS_CHASSIS]
    if not chassis:
        return
    idx = sorted(chassis, key=lambda i: _int(i.split(".")[0]))[0]
    vals = await client.get([f"{oid}.{idx}" for oid in (mibs.ENT_MODEL, mibs.ENT_SERIAL, mibs.ENT_SW_REV, mibs.ENT_HW_REV, mibs.ENT_DESCR)])
    sw.model = _text(vals.get(f"{mibs.ENT_MODEL}.{idx}")) or _text(vals.get(f"{mibs.ENT_DESCR}.{idx}"))
    sw.serial = _text(vals.get(f"{mibs.ENT_SERIAL}.{idx}"))
    sw.hardware_revision = _text(vals.get(f"{mibs.ENT_HW_REV}.{idx}"))
    sw.software_version = _text(vals.get(f"{mibs.ENT_SW_REV}.{idx}")) or sw.software_version


async def _walk_col(client: SnmpClient, oid: str) -> dict[str, Any]:
    return column(await client.walk(oid), oid)


async def _interfaces(sw: SwitchInfo, client: SnmpClient) -> None:
    descr = await _walk_col(client, mibs.IF_DESCR)
    cols = {
        name: await _walk_col(client, oid)
        for name, oid in (
            ("type", mibs.IF_TYPE), ("speed", mibs.IF_SPEED), ("phys", mibs.IF_PHYS_ADDRESS),
            ("admin", mibs.IF_ADMIN_STATUS), ("oper", mibs.IF_OPER_STATUS), ("last", mibs.IF_LAST_CHANGE),
            ("in_err", mibs.IF_IN_ERRORS), ("out_err", mibs.IF_OUT_ERRORS),
            ("in_disc", mibs.IF_IN_DISCARDS), ("out_disc", mibs.IF_OUT_DISCARDS),
            ("name", mibs.IF_NAME), ("high", mibs.IF_HIGH_SPEED), ("alias", mibs.IF_ALIAS),
        )
    }
    for idx, d in descr.items():
        get = lambda col: cols[col].get(idx)  # noqa: E731
        high = _int(get("high"))
        port = Port(
            if_index=_int(idx),
            descr=_text(d),
            name=_text(get("name")) or _text(d),
            alias=_text(get("alias")),
            if_type=_int(get("type")),
            admin_status=mibs.IF_STATUS.get(_int(get("admin")), "unknown"),
            oper_status=mibs.IF_STATUS.get(_int(get("oper")), "unknown"),
            speed_mbps=high if high else _int(get("speed")) // 1_000_000,
            mac=normalize_mac(get("phys")) if get("phys") else "",
            last_change_ticks=_int(get("last")),
            in_errors=_int(get("in_err")),
            out_errors=_int(get("out_err")),
            in_discards=_int(get("in_disc")),
            out_discards=_int(get("out_disc")),
        )
        sw.ports.append(port)
    sw.ports.sort(key=lambda p: p.if_index)


async def _ethernet(ports: dict[int, Port], client: SnmpClient) -> None:
    duplex = await _walk_col(client, mibs.DOT3_DUPLEX_STATUS)
    fcs = await _walk_col(client, mibs.DOT3_FCS_ERRORS)
    align = await _walk_col(client, mibs.DOT3_ALIGNMENT_ERRORS)
    late = await _walk_col(client, mibs.DOT3_LATE_COLLISIONS)
    for idx, port in ports.items():
        key = str(idx)
        if key in duplex:
            port.duplex = mibs.DUPLEX.get(_int(duplex[key]), "unknown")
        port.fcs_errors = _int(fcs.get(key))
        port.alignment_errors = _int(align.get(key))
        port.late_collisions = _int(late.get(key))


async def _port_vlans(ports: dict[int, Port], client: SnmpClient) -> None:
    for idx, vlan in (await _walk_col(client, mibs.VM_VLAN)).items():
        port = ports.get(_int(idx))
        if port is not None:
            port.vlan = _int(vlan)


async def _mac_table(ports: dict[int, Port], client: SnmpClient) -> None:
    """Fill ``Port.macs``. Tries Q-BRIDGE first, then per-VLAN BRIDGE-MIB (IOS style)."""
    found: dict[int, set[str]] = {}

    def add(if_index: int, mac: str) -> None:
        if if_index in ports:
            found.setdefault(if_index, set()).add(mac)

    async def bridge_ports(vlan):
        rows = column(await client.walk(mibs.DOT1D_BASE_PORT_IFINDEX, vlan=vlan), mibs.DOT1D_BASE_PORT_IFINDEX)
        return {_int(k): _int(v) for k, v in rows.items()}

    # Q-BRIDGE-MIB: one walk covers all VLANs.
    q_port = column(await client.walk(mibs.DOT1Q_TP_FDB_PORT), mibs.DOT1Q_TP_FDB_PORT)
    if q_port:
        q_status = column(await client.walk(mibs.DOT1Q_TP_FDB_STATUS), mibs.DOT1Q_TP_FDB_STATUS)
        bp_map = await bridge_ports(None)
        for idx, bport in q_port.items():
            if _int(q_status.get(idx), mibs.FDB_STATUS_LEARNED) != mibs.FDB_STATUS_LEARNED:
                continue
            add(bp_map.get(_int(bport), _int(bport)), _mac_from_index(idx))
    else:
        # BRIDGE-MIB: default context first, then each active VLAN (community@vlan / vlan-N context).
        vlans: list = [None]
        vtp = column(await client.walk(mibs.VTP_VLAN_STATE), mibs.VTP_VLAN_STATE)
        vlans += sorted({_int(i.split(".")[-1]) for i, state in vtp.items() if _int(state) == 1} - _RESERVED_VLANS)
        for vlan in vlans:
            try:
                fdb = column(await client.walk(mibs.DOT1D_TP_FDB_PORT, vlan=vlan), mibs.DOT1D_TP_FDB_PORT)
                if not fdb:
                    continue
                status = column(await client.walk(mibs.DOT1D_TP_FDB_STATUS, vlan=vlan), mibs.DOT1D_TP_FDB_STATUS)
                bp_map = await bridge_ports(vlan)
            except Exception as exc:
                log.debug("vlan %s fdb walk failed: %s", vlan, exc)
                continue
            for idx, bport in fdb.items():
                if _int(status.get(idx), mibs.FDB_STATUS_LEARNED) != mibs.FDB_STATUS_LEARNED:
                    continue
                add(bp_map.get(_int(bport), _int(bport)), _mac_from_index(idx))

    for if_index, macs in found.items():
        ports[if_index].macs = sorted(macs)


async def _arp(sw: SwitchInfo, client: SnmpClient) -> None:
    for idx, mac in (await _walk_col(client, mibs.IP_NET_TO_MEDIA_PHYS)).items():
        parts = idx.split(".")
        if len(parts) >= 5 and mac:
            sw.arp[".".join(parts[-4:])] = normalize_mac(mac)


def _decode_port_id(value: Any) -> str:
    """LLDP port ids are sometimes raw MACs; render those readably."""
    if isinstance(value, (bytes, bytearray)) and len(value) == 6 and not all(32 <= b < 127 for b in value):
        return normalize_mac(value)
    return _text(value)


async def _lldp(sw: SwitchInfo, ports: dict[int, Port], client: SnmpClient) -> None:
    names = await _walk_col(client, mibs.LLDP_REM_SYS_NAME)
    if not names:
        return
    port_ids = await _walk_col(client, mibs.LLDP_REM_PORT_ID)
    port_descs = await _walk_col(client, mibs.LLDP_REM_PORT_DESC)
    loc_desc = await _walk_col(client, mibs.LLDP_LOC_PORT_DESC)
    loc_id = await _walk_col(client, mibs.LLDP_LOC_PORT_ID)
    man = await _walk_col(client, mibs.LLDP_REM_MAN_ADDR_IF_SUBTYPE)

    addr_by_rem: dict[str, str] = {}
    for idx in man:
        p = idx.split(".")
        # timeMark.localPort.remIndex.addrSubtype(1=ipv4).len(4).a.b.c.d
        if len(p) == 9 and p[3] == "1" and p[4] == "4":
            addr_by_rem.setdefault(".".join(p[:3]), ".".join(p[5:]))

    by_name = {}
    for port in ports.values():
        for key in (port.name, port.descr):
            if key:
                by_name[key.lower()] = port

    for idx, name in names.items():
        parts = idx.split(".")
        if len(parts) < 3:
            continue
        local_num = parts[1]
        port = None
        for label in (_text(loc_desc.get(local_num)), _text(loc_id.get(local_num))):
            port = by_name.get(label.lower()) if label else None
            if port:
                break
        port = port or ports.get(_int(local_num))
        if port is None:
            continue
        port.neighbors.append(Neighbor(
            protocol="lldp",
            local_port=port.name,
            remote_name=_text(name),
            remote_port=_text(port_descs.get(idx)) or _decode_port_id(port_ids.get(idx)),
            remote_address=addr_by_rem.get(".".join(parts[:3]), ""),
        ))


async def _cdp(ports: dict[int, Port], client: SnmpClient) -> None:
    device_ids = await _walk_col(client, mibs.CDP_CACHE_DEVICE_ID)
    if not device_ids:
        return
    dev_ports = await _walk_col(client, mibs.CDP_CACHE_DEVICE_PORT)
    platforms = await _walk_col(client, mibs.CDP_CACHE_PLATFORM)
    addrs = await _walk_col(client, mibs.CDP_CACHE_ADDRESS)
    for idx, dev_id in device_ids.items():
        port = ports.get(_int(idx.split(".")[0]))
        if port is None:
            continue
        raw = addrs.get(idx)
        address = ".".join(str(b) for b in raw) if isinstance(raw, (bytes, bytearray)) and len(raw) == 4 else ""
        name = _text(dev_id)
        platform = _text(platforms.get(idx))
        # Same neighbour seen over LLDP: enrich that entry instead of listing it twice.
        existing = next((n for n in port.neighbors if n.remote_name.split(".")[0] == name.split(".")[0]), None)
        if existing is not None:
            existing.remote_platform = existing.remote_platform or platform
            existing.remote_address = existing.remote_address or address
            continue
        port.neighbors.append(Neighbor(
            protocol="cdp",
            local_port=port.name,
            remote_name=name,
            remote_port=_text(dev_ports.get(idx)),
            remote_platform=platform,
            remote_address=address,
        ))


_INFRA_HINTS = ("stratix", "switch", "cisco", "ws-c", "ie-", "1783-")
UPLINK_MAC_COUNT = 8


def _mark_uplinks(sw: SwitchInfo) -> None:
    """A port facing another switch must not be used to locate end devices."""
    for port in sw.ports:
        for n in port.neighbors:
            hay = f"{n.remote_name} {n.remote_platform}".lower()
            if n.protocol == "cdp" or any(h in hay for h in _INFRA_HINTS) or len(port.macs) >= UPLINK_MAC_COUNT:
                port.is_uplink = True

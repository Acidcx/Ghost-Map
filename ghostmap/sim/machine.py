"""A simulated machine cell used for demo mode, the ListIdentity simulator and tests.

All names, serials and revisions here are made up for demonstration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from ghostmap.analysis.diagnostics import run_diagnostics
from ghostmap.analysis.topology import build_inventory
from ghostmap.collectors.stratix import collect_switch
from ghostmap.models import DiscoveredIdentity, ScanResult
from ghostmap.protocols import mibs
from ghostmap.protocols.enip import build_list_identity_response, parse_list_identity
from ghostmap.protocols.snmp import FakeSnmpClient

SWITCH_IP = "192.168.1.2"
SWITCH_MAC = "00:00:bc:10:00:02"
MGMT_VLAN_IFINDEX = 100
DEMO_BASELINE = {
    "firmware": [
        {"product_name": "PowerFlex 525", "revision": "7.001", "note": "Line standard per machine spec."},
        {"product_name": "1756-EN2T/D", "revision": "11.002"},
    ]
}


@dataclass
class SimDevice:
    ip: Optional[str]
    mac: str
    port: str  # switch interface name
    cip: Optional[dict[str, Any]] = None  # kwargs for build_list_identity_response (minus ip)


@dataclass
class SimPort:
    if_index: int
    name: str
    alias: str = ""
    speed_mbps: int = 100
    oper_up: bool = True
    duplex: int = 3  # 2 half, 3 full
    fcs_errors: int = 0
    late_collisions: int = 0
    vlan: int = 10
    extra_macs: list[str] = field(default_factory=list)  # e.g. uplink MACs


def _cip(product_name, vendor_id=1, device_type=0x0C, product_code=1, revision=(1, 1), status=0x0064,
         serial=0, state=3):
    return dict(product_name=product_name, vendor_id=vendor_id, device_type=device_type,
                product_code=product_code, revision=revision, status=status, serial=serial, state=state)


def demo_machine(variant: str = "today") -> tuple[list[SimDevice], list[SimPort]]:
    """``variant`` is "baseline" (as commissioned) or "today" (with problems to find)."""
    today = variant == "today"
    devices = [
        SimDevice(SWITCH_IP, SWITCH_MAC, "", _cip("Stratix 5700 1783-BMS10CGL", device_type=0x2C, product_code=101,
                                                   revision=(15, 2), status=0x0030, serial=0x40A1B2C3)),
        SimDevice("192.168.1.10", "00:00:bc:aa:00:10", "Fa1/1", _cip("1756-EN2T/D", product_code=166,
                                                                     revision=(11, 2), status=0x0060, serial=0x00C0FFEE)),
        SimDevice("192.168.1.11", "00:00:bc:aa:00:11", "Gi1/1", _cip("1756-L83E/B", device_type=0x0E, product_code=169,
                                                                     revision=(33, 11), status=0x0160 if today else 0x0060, serial=0x00D15EA5)),
        SimDevice("192.168.1.20", "00:00:bc:bb:00:20", "Fa1/2", _cip("PowerFlex 755", device_type=0x02, product_code=2192,
                                                                     revision=(14, 1), status=0x0065 if today else 0x0061,
                                                                     serial=0x12340020)),
        SimDevice("192.168.1.21", "00:00:bc:bb:00:21", "Fa1/3", _cip("PowerFlex 525", device_type=0x02, product_code=2210,
                                                                     revision=(7, 1), status=0x0065, serial=0x12340021)),
        SimDevice("192.168.1.22", "00:00:bc:bb:00:22", "Fa1/4", _cip("PowerFlex 525", device_type=0x02, product_code=2210,
                                                                     revision=(5, 1) if today else (7, 1), status=0x0065,
                                                                     serial=0x12340022 if today else 0x12340099)),
        SimDevice("192.168.1.30", "00:00:bc:cc:00:30", "Fa1/5", _cip("1734-AENTR/B", product_code=146, revision=(6, 12),
                                                                     status=0x0425 if today else 0x0065, serial=0x0BADF00D,
                                                                     state=4 if today else 3)),
        SimDevice("192.168.1.31", "00:00:bc:cc:00:31", "Fa1/5", _cip("1734-AENTR/B", product_code=146, revision=(6, 12),
                                                                     status=0x0025 if today else 0x0065, serial=0x0BADF00E)),
        SimDevice("192.168.1.40", "00:00:bc:dd:00:40", "Fa1/6", _cip("PanelView 5510", device_type=0x18, product_code=300,
                                                                     revision=(6, 11), status=0x0030, serial=0x00FACADE)),
    ]
    if today:
        devices += [
            SimDevice("192.168.1.50", "00:d0:24:01:02:50", "Fa1/7"),  # vision camera, no CIP, seen via ARP
            SimDevice(None, "00:1b:1b:de:ad:01", "Fa1/6"),  # laptop on an unmanaged switch at the HMI, no IP known
        ]
    ports = [
        SimPort(1, "Gi1/1", "CONTROLLER", speed_mbps=1000),
        SimPort(2, "Fa1/1", "CHASSIS-EN2T"),
        SimPort(3, "Fa1/2", "VFD-MAIN-CONV", duplex=2 if today else 3, fcs_errors=1532 if today else 0,
                late_collisions=17 if today else 0),
        SimPort(4, "Fa1/3", "VFD-INFEED"),
        SimPort(5, "Fa1/4", "VFD-OUTFEED"),
        SimPort(6, "Fa1/5", "RIO-ZONE1"),
        SimPort(7, "Fa1/6", "HMI"),
        SimPort(8, "Fa1/7", "CAMERA", oper_up=today),
        SimPort(9, "Fa1/8", "ROBOT", oper_up=False),
        SimPort(10, "Gi1/2", "UPLINK-PLANT", speed_mbps=1000, vlan=1,
                extra_macs=[f"00:50:56:00:00:{i:02x}" for i in range(1, 25)]),
    ]
    return devices, ports


def _mac_index(mac: str) -> str:
    return ".".join(str(int(p, 16)) for p in mac.split(":"))


def _ip_bytes(ip: str) -> bytes:
    return bytes(int(p) for p in ip.split("."))


def switch_oids(devices: list[SimDevice], ports: list[SimPort]) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """OID map for a simulated Stratix 5700 (IOS style, per-VLAN BRIDGE-MIB)."""
    d: dict[str, Any] = {
        mibs.SYS_DESCR: b"Cisco IOS Software, IE2000 Software (IE2000-UNIVERSALK9-M), Version 15.2(8)E4, "
                        b"RELEASE SOFTWARE (fc2) Stratix 5700",
        mibs.SYS_OBJECT_ID: "1.3.6.1.4.1.9.1.1824",
        mibs.SYS_UPTIME: 360000 * 24 * 12,  # 12 days in ticks
        mibs.SYS_CONTACT: b"Controls Engineering",
        mibs.SYS_NAME: b"CELL1-SW01",
        mibs.SYS_LOCATION: b"Packaging Line 1 - Panel CP-101",
        f"{mibs.ENT_CLASS}.1001": 3,
        f"{mibs.ENT_CLASS}.1002": 9,
        f"{mibs.ENT_MODEL}.1001": b"1783-BMS10CGL",
        f"{mibs.ENT_SERIAL}.1001": b"FOC2049X0AB",
        f"{mibs.ENT_SW_REV}.1001": b"15.2(8)E4",
        f"{mibs.ENT_HW_REV}.1001": b"V03",
        f"{mibs.ENT_DESCR}.1001": b"Stratix 5700 10 port managed switch",
        f"{mibs.VTP_VLAN_STATE}.1.1": 1,
        f"{mibs.VTP_VLAN_STATE}.1.10": 1,
        f"{mibs.VTP_VLAN_STATE}.1.1002": 1,
    }
    all_ifs = [(p.if_index, p.name, p.alias, 6, p.speed_mbps, p.oper_up) for p in ports]
    all_ifs.append((MGMT_VLAN_IFINDEX, "Vl10", "MGMT", 53, 1000, True))
    for idx, name, alias, if_type, speed, up in all_ifs:
        long_name = name.replace("Gi", "GigabitEthernet").replace("Fa", "FastEthernet").replace("Vl", "Vlan")
        d[f"{mibs.IF_DESCR}.{idx}"] = long_name.encode()
        d[f"{mibs.IF_NAME}.{idx}"] = name.encode()
        d[f"{mibs.IF_ALIAS}.{idx}"] = alias.encode()
        d[f"{mibs.IF_TYPE}.{idx}"] = if_type
        d[f"{mibs.IF_SPEED}.{idx}"] = speed * 1_000_000 if up else 10_000_000
        d[f"{mibs.IF_HIGH_SPEED}.{idx}"] = speed if up else 10
        d[f"{mibs.IF_PHYS_ADDRESS}.{idx}"] = bytes.fromhex(f"0000bc1001{idx:02x}")
        d[f"{mibs.IF_ADMIN_STATUS}.{idx}"] = 1
        d[f"{mibs.IF_OPER_STATUS}.{idx}"] = 1 if up else 2
        d[f"{mibs.IF_LAST_CHANGE}.{idx}"] = 1200
        for oid in (mibs.IF_IN_ERRORS, mibs.IF_OUT_ERRORS, mibs.IF_IN_DISCARDS, mibs.IF_OUT_DISCARDS):
            d[f"{oid}.{idx}"] = 0
    vlan_data: dict[int, dict[str, Any]] = {1: {}, 10: {}}
    for p in ports:
        d[f"{mibs.DOT3_DUPLEX_STATUS}.{p.if_index}"] = p.duplex if p.oper_up else 1
        d[f"{mibs.DOT3_FCS_ERRORS}.{p.if_index}"] = p.fcs_errors
        d[f"{mibs.DOT3_ALIGNMENT_ERRORS}.{p.if_index}"] = p.fcs_errors // 4
        d[f"{mibs.DOT3_LATE_COLLISIONS}.{p.if_index}"] = p.late_collisions
        d[f"{mibs.IF_IN_ERRORS}.{p.if_index}"] = p.fcs_errors + p.fcs_errors // 4
        d[f"{mibs.VM_VLAN}.{p.if_index}"] = p.vlan
        for vlan in vlan_data:
            vlan_data[vlan][f"{mibs.DOT1D_BASE_PORT_IFINDEX}.{p.if_index}"] = p.if_index
        for mac in p.extra_macs:
            vlan_data[p.vlan][f"{mibs.DOT1D_TP_FDB_PORT}.{_mac_index(mac)}"] = p.if_index
            vlan_data[p.vlan][f"{mibs.DOT1D_TP_FDB_STATUS}.{_mac_index(mac)}"] = 3
    by_name = {p.name: p for p in ports}
    for dev in devices:
        port = by_name.get(dev.port)
        if port is None or not port.oper_up:
            continue
        vlan_data[port.vlan][f"{mibs.DOT1D_TP_FDB_PORT}.{_mac_index(dev.mac)}"] = port.if_index
        vlan_data[port.vlan][f"{mibs.DOT1D_TP_FDB_STATUS}.{_mac_index(dev.mac)}"] = 3
        if dev.ip and dev.cip and dev.cip["device_type"] == 0x0E:  # switch ARP cache only knows a few hosts
            d[f"{mibs.IP_NET_TO_MEDIA_PHYS}.{MGMT_VLAN_IFINDEX}.{dev.ip}"] = bytes.fromhex(dev.mac.replace(":", ""))
    # Uplink neighbour seen via LLDP and CDP.
    d[f"{mibs.LLDP_LOC_PORT_DESC}.10"] = b"GigabitEthernet1/2"
    d[f"{mibs.LLDP_LOC_PORT_ID}.10"] = b"Gi1/2"
    d[f"{mibs.LLDP_REM_SYS_NAME}.0.10.1"] = b"PLANT-CORE.example.local"
    d[f"{mibs.LLDP_REM_PORT_ID}.0.10.1"] = b"Gi1/0/24"
    d[f"{mibs.LLDP_REM_PORT_DESC}.0.10.1"] = b"GigabitEthernet1/0/24"
    d[f"{mibs.LLDP_REM_MAN_ADDR_IF_SUBTYPE}.0.10.1.1.4.10.10.0.1"] = 2
    d[f"{mibs.CDP_CACHE_DEVICE_ID}.10.1"] = b"PLANT-CORE.example.local"
    d[f"{mibs.CDP_CACHE_DEVICE_PORT}.10.1"] = b"GigabitEthernet1/0/24"
    d[f"{mibs.CDP_CACHE_PLATFORM}.10.1"] = b"cisco IE-5000-12S12P-10G"
    d[f"{mibs.CDP_CACHE_ADDRESS}.10.1"] = _ip_bytes("10.10.0.1")
    return d, vlan_data


def identity_reply(dev: SimDevice, context: bytes = b"\0" * 8) -> bytes:
    assert dev.cip and dev.ip
    return build_list_identity_response(ip=dev.ip, context=context, **dev.cip)


def build_demo_scan(variant: str = "today", scan_id: Optional[str] = None) -> ScanResult:
    """Run the real collector/correlation/diagnostics pipeline against the simulated cell."""
    devices, ports = demo_machine(variant)
    data, vlan_data = switch_oids(devices, ports)
    switch = asyncio.run(collect_switch(SWITCH_IP, FakeSnmpClient(data, vlan_data)))

    identities = [
        DiscoveredIdentity(source_ip=dev.ip, identity=ident)
        for dev in devices if dev.cip and dev.ip
        for ident in parse_list_identity(identity_reply(dev))
    ]
    local_arp = {dev.ip: dev.mac for dev in devices if dev.ip}
    inventory = build_inventory(identities, [switch], local_arp)
    findings = run_diagnostics(identities, inventory, [switch], baseline=DEMO_BASELINE)
    stamp = "2026-09-01T07:30:00+00:00" if variant == "baseline" else "2026-10-05T07:30:00+00:00"
    return ScanResult(
        id=scan_id or f"demo-{variant}",
        started=stamp,
        finished=stamp,
        params={"label": f"DEMO - Packaging Line 1 ({'as commissioned' if variant == 'baseline' else 'today'})",
                "targets": ["192.168.1.0/24"], "switches": [SWITCH_IP], "demo": True},
        identities=identities,
        devices=inventory,
        switches=[switch],
        findings=findings,
        log=["Demo data - generated from the built-in simulated machine cell."],
    )

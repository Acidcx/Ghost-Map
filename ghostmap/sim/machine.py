"""A simulated machine cell used for demo mode, the ListIdentity simulator and tests.

All names, serials and revisions here are made up for demonstration.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ghostmap.analysis.diagnostics import run_diagnostics
from ghostmap.analysis.topology import build_inventory
from ghostmap.collectors.portstats import sample_traffic
from ghostmap.collectors.stratix import collect_switch
from ghostmap.models import DiscoveredIdentity, ScanResult
from ghostmap.protocols import mibs
from ghostmap.protocols.enip import build_list_identity_response, parse_list_identity
from ghostmap.protocols.snmp import FakeSnmpClient, oid_suffix

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

    traffic: dict[str, "Flow"] = field(default_factory=dict)  # counter name (portstats.COUNTERS) -> rate per second


@dataclass
class Flow:
    """A counter's rate per second: ``base``, plus a slow swing of up to ``swing`` either way over ``period``
    seconds (highest at t = 0), plus ``burst`` extra for ``burst_for`` seconds out of every ``burst_every``
    (a broadcast storm in the demo)."""

    base: float
    swing: float = 0.0
    period: float = 600.0
    burst: float = 0.0
    burst_every: float = 1800.0
    burst_for: float = 60.0

    def total(self, t: float) -> float:
        """Counter value after ``t`` seconds (the integral of the rate), so readings always count up."""
        v = self.base * t
        if self.swing:
            w = 2 * math.pi / self.period
            v += self.swing * math.sin(w * t) / w
        if self.burst:
            cycles, rest = divmod(t, self.burst_every)
            v += self.burst * (cycles * self.burst_for + min(rest, self.burst_for))
        return v


def _cip(product_name, vendor_id=1, device_type=0x0C, product_code=1, revision=(1, 1), status=0x0064,
         serial=0, state=3):
    return dict(product_name=product_name, vendor_id=vendor_id, device_type=device_type,
                product_code=product_code, revision=revision, status=status, serial=serial, state=state)


def demo_machine(variant: str = "today", chain: bool = False) -> tuple[list[SimDevice], list[SimPort]]:
    """``variant`` is "baseline" (as commissioned) or "today" (with problems to find). ``chain`` adds port Gi1/3
    to the second switch, CELL1-SW02 (the traffic monitor's demo; the demo scans have one switch)."""
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
    if chain:
        ports.append(SimPort(11, "Gi1/3", "TO-SW02", speed_mbps=1000,
                             extra_macs=[d.mac for d in demo_sw02(variant)[0]]))
    for p in ports:
        p.traffic = _port_traffic(p, today)
    return devices, ports


def _mbit(mbps: float) -> float:
    return mbps * 1e6 / 8  # octets per second


def _port_traffic(p: SimPort, today: bool) -> dict[str, Flow]:
    """Ordinary machine traffic per port, plus today's problems: the laptop's unmanaged switch at the HMI
    is looped (broadcast storm), the camera streams at most of its 100 Mbps, and the main VFD's cable is
    still taking errors."""
    if not p.oper_up:
        return {}
    mbps_in, mbps_out, mcast_in, mcast_out = {
        "CONTROLLER": (4.2, 3.1, 1800, 350), "CHASSIS-EN2T": (2.5, 2.0, 900, 0), "RIO-ZONE1": (1.1, 0.8, 500, 0),
        "HMI": (0.9, 1.6, 0, 0), "CAMERA": (60.0 if today else 6.0, 0.4, 0, 0), "UPLINK-PLANT": (12.0, 8.0, 150, 40),
        "TO-SW02": (3.1, 4.4, 1300, 2100), "TO-SW01": (4.4, 3.1, 2100, 1300), "DRIVES-DAISY": (2.6, 2.2, 1200, 0),
        "RIO-ZONE2": (1.0, 0.7, 450, 0), "SAFETY-IO": (0.5, 0.4, 0, 0),
    }.get(p.alias, (0.4, 0.3, 0, 0))
    link = p.alias.startswith(("UPLINK", "TO-"))
    bcast_in = 25.0 if link else 0.4
    f = {"in_octets": Flow(_mbit(mbps_in), swing=_mbit(mbps_in) * 0.15), "out_octets": Flow(_mbit(mbps_out)),
         "in_mcast": Flow(mcast_in), "out_mcast": Flow(mcast_out), "in_bcast": Flow(bcast_in), "out_bcast": Flow(26.0),
         "in_errors": Flow(0), "in_discards": Flow(0), "out_discards": Flow(0)}
    if today and p.alias == "HMI":
        f["in_bcast"] = Flow(bcast_in, burst=4200)
        f["in_octets"] = Flow(_mbit(mbps_in), burst=4200 * 64)
    if today:
        f["out_bcast"] = Flow(26.0, burst=4200)  # the storm floods every port on the VLAN...
    if today and p.alias == "TO-SW01":  # ...and reaches the next switch down the chain
        f["in_bcast"] = Flow(bcast_in, burst=4200)
    if today and p.alias == "CAMERA":  # 85 Mbps at the start, down to 35 half an hour later
        f["in_octets"] = Flow(_mbit(60.0), swing=_mbit(25.0), period=3600.0)
    if today and p.alias == "VFD-MAIN-CONV":
        f["in_errors"] = Flow(2.0)
    return f


def _mac_index(mac: str) -> str:
    return ".".join(str(int(p, 16)) for p in mac.split(":"))


def _ip_bytes(ip: str) -> bytes:
    return bytes(int(p) for p in ip.split("."))


SW01 = {"name": "CELL1-SW01", "model": "1783-BMS10CGL", "serial": "FOC2049X0AB", "mac_prefix": "0000bc1001",
        "location": "Packaging Line 1 - Panel CP-101", "descr": "Stratix 5700 10 port managed switch",
        "psu_fault": True, "neighbors": [
            {"if_index": 10, "port": "Gi1/2", "name": "PLANT-CORE.example.local", "remote_port": "Gi1/0/24",
             "platform": "cisco IE-5000-12S12P-10G", "ip": "10.10.0.1"}]}
SW02_IP = "192.168.1.3"
SW02_MAC = "00:00:bc:10:00:03"
SW02 = {"name": "CELL1-SW02", "model": "1783-BMS06SGL", "serial": "FOC2112X1CD", "mac_prefix": "0000bc1002",
        "location": "Packaging Line 1 - Panel CP-102 (infeed)", "descr": "Stratix 5700 6 port managed switch",
        "psu_fault": False, "cpu_less": 8, "neighbors": [
            {"if_index": 1, "port": "Gi1/1", "name": "CELL1-SW01", "remote_port": "Gi1/3",
             "platform": "cisco Stratix 5700", "ip": SWITCH_IP}]}
SW01_TO_SW02 = {"if_index": 11, "port": "Gi1/3", "name": "CELL1-SW02", "remote_port": "Gi1/1",
                "platform": "cisco Stratix 5700", "ip": SW02_IP}


def demo_sw02(variant: str = "today") -> tuple[list[SimDevice], list[SimPort]]:
    """A second switch daisy-chained off CELL1-SW01 Gi1/3, as machines are often wired: its drives hang off one
    port in a daisy chain (each PowerFlex 755 passes the line on through its two-port switch)."""
    today = variant == "today"
    drives = [SimDevice(f"192.168.1.{60 + i}", f"00:00:bc:ee:00:{60 + i:02x}", "Fa1/1",
                        _cip("PowerFlex 755", device_type=0x02, product_code=2192, revision=(14, 1), status=0x0065,
                             serial=0x12340060 + i)) for i in range(6)]
    devices = [SimDevice(SW02_IP, SW02_MAC, "", _cip("Stratix 5700 1783-BMS06SGL", device_type=0x2C, product_code=102,
                                                     revision=(15, 2), status=0x0030, serial=0x40A1B2C4)),
               *drives,
               SimDevice("192.168.1.70", "00:00:bc:cc:00:70", "Fa1/2", _cip("1734-AENTR/B", product_code=146,
                                                                            revision=(6, 12), status=0x0065, serial=0x0BADF070)),
               SimDevice("192.168.1.71", "00:00:bc:cc:00:71", "Fa1/3", _cip("1734-AENTR/B", product_code=146,
                                                                            revision=(6, 12), status=0x0065, serial=0x0BADF071))]
    sw01_devices, _ = demo_machine(variant)
    ports = [
        SimPort(1, "Gi1/1", "TO-SW01", speed_mbps=1000,
                extra_macs=[d.mac for d in sw01_devices] + [f"00:50:56:00:00:{i:02x}" for i in range(1, 25)]),
        SimPort(2, "Fa1/1", "DRIVES-DAISY"),
        SimPort(3, "Fa1/2", "RIO-ZONE2"),
        SimPort(4, "Fa1/3", "SAFETY-IO"),
        SimPort(5, "Fa1/4", "SPARE", oper_up=False),
    ]
    for p in ports:
        p.traffic = _port_traffic(p, today)
    return devices, ports


def demo_device_names(variant: str = "today") -> dict[str, str]:
    """MAC -> "product IP" for both demo switches' devices, as a scan would name them."""
    return {d.mac: f"{d.cip['product_name']} {d.ip}" if d.cip else (d.ip or "")
            for d in demo_machine(variant)[0] + demo_sw02(variant)[0]}


def switch_oids(devices: list[SimDevice], ports: list[SimPort], variant: str = "today",
                ident: Optional[dict] = None) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """OID map for a simulated Stratix 5700 (IOS style, per-VLAN BRIDGE-MIB). ``ident`` is SW01 (default) or SW02."""
    today = variant == "today"
    ident = ident or SW01
    d: dict[str, Any] = {
        mibs.SYS_DESCR: b"Cisco IOS Software, IE2000 Software (IE2000-UNIVERSALK9-M), Version 15.2(8)E4, "
                        b"RELEASE SOFTWARE (fc2) Stratix 5700",
        mibs.SYS_OBJECT_ID: "1.3.6.1.4.1.9.1.1824",
        mibs.SYS_UPTIME: 360000 * 24 * 12,  # 12 days in ticks
        mibs.SYS_CONTACT: b"Controls Engineering",
        mibs.SYS_NAME: ident["name"].encode(),
        mibs.SYS_LOCATION: ident["location"].encode(),
        f"{mibs.ENT_CLASS}.1001": 3,
        f"{mibs.ENT_CLASS}.1002": 9,
        f"{mibs.ENT_MODEL}.1001": ident["model"].encode(),
        f"{mibs.ENT_SERIAL}.1001": ident["serial"].encode(),
        f"{mibs.ENT_SW_REV}.1001": b"15.2(8)E4",
        f"{mibs.ENT_HW_REV}.1001": b"V03",
        f"{mibs.ENT_DESCR}.1001": ident["descr"].encode(),
        f"{mibs.VTP_VLAN_STATE}.1.1": 1,
        f"{mibs.VTP_VLAN_STATE}.1.10": 1,
        f"{mibs.VTP_VLAN_STATE}.1.1002": 1,
    }
    # CPU, memory and environment (classic IOS: CISCO-PROCESS-MIB, CISCO-MEMORY-POOL-MIB, CISCO-ENVMON-MIB).
    d.update({
        f"{mibs.CPM_CPU_5SEC_REV}.1": (34 if today else 9) - ident.get("cpu_less", 0),
        f"{mibs.CPM_CPU_1MIN_REV}.1": (21 if today else 8) - ident.get("cpu_less", 0) // 2,
        f"{mibs.CPM_CPU_5MIN_REV}.1": (17 if today else 8) - ident.get("cpu_less", 0) // 2,
        f"{mibs.MEM_POOL_NAME}.1": b"Processor", f"{mibs.MEM_POOL_USED}.1": 58_000_000, f"{mibs.MEM_POOL_FREE}.1": 96_000_000,
        f"{mibs.MEM_POOL_NAME}.2": b"I/O", f"{mibs.MEM_POOL_USED}.2": 9_000_000, f"{mibs.MEM_POOL_FREE}.2": 7_000_000,
        f"{mibs.ENV_TEMP_DESCR}.1006": b"SW#1, Sensor#1, GREEN", f"{mibs.ENV_TEMP_VALUE}.1006": 51 if today else 44,
        f"{mibs.ENV_TEMP_THRESHOLD}.1006": 80, f"{mibs.ENV_TEMP_STATE}.1006": 1,
        f"{mibs.ENV_SUPPLY_DESCR}.1003": b"Power Supply A (DC 24V)", f"{mibs.ENV_SUPPLY_STATE}.1003": 1,
        f"{mibs.ENV_SUPPLY_DESCR}.1004": b"Power Supply B (DC 24V)", f"{mibs.ENV_SUPPLY_STATE}.1004": 6 if today and ident["psu_fault"] else 1,
    })
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
        d[f"{mibs.IF_PHYS_ADDRESS}.{idx}"] = bytes.fromhex(f"{ident['mac_prefix']}{idx:02x}")
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
    # Spanning tree: the default context is VLAN 1's instance; VLAN 10 (the machine) changed recently today.
    root = bytes.fromhex("8001") + bytes.fromhex("00005e0a0001")
    for ctx, vlan in ((d, 1), (vlan_data[1], 1), (vlan_data[10], 10)):
        changes, since = (3, 12 * 86400) if vlan == 1 or not today else (9, 7 * 60)
        ctx[f"{mibs.STP_TOP_CHANGES}.0"] = changes
        ctx[f"{mibs.STP_TIME_SINCE_CHANGE}.0"] = since * 100
        ctx[f"{mibs.STP_DESIGNATED_ROOT}.0"] = root
    by_name = {p.name: p for p in ports}
    for dev in devices:
        port = by_name.get(dev.port)
        if port is None or not port.oper_up:
            continue
        vlan_data[port.vlan][f"{mibs.DOT1D_TP_FDB_PORT}.{_mac_index(dev.mac)}"] = port.if_index
        vlan_data[port.vlan][f"{mibs.DOT1D_TP_FDB_STATUS}.{_mac_index(dev.mac)}"] = 3
        if dev.ip and dev.cip and dev.cip["device_type"] == 0x0E:  # switch ARP cache only knows a few hosts
            d[f"{mibs.IP_NET_TO_MEDIA_PHYS}.{MGMT_VLAN_IFINDEX}.{dev.ip}"] = bytes.fromhex(dev.mac.replace(":", ""))
    # Links to other switches, seen via LLDP and CDP.
    for n in ident["neighbors"]:
        if not any(p.if_index == n["if_index"] for p in ports):
            continue
        i, long_port = n["if_index"], n["port"].replace("Gi", "GigabitEthernet").replace("Fa", "FastEthernet")
        d[f"{mibs.LLDP_LOC_PORT_DESC}.{i}"] = long_port.encode()
        d[f"{mibs.LLDP_LOC_PORT_ID}.{i}"] = n["port"].encode()
        d[f"{mibs.LLDP_REM_SYS_NAME}.0.{i}.1"] = n["name"].encode()
        d[f"{mibs.LLDP_REM_PORT_ID}.0.{i}.1"] = n["remote_port"].encode()
        d[f"{mibs.LLDP_REM_PORT_DESC}.0.{i}.1"] = n["remote_port"].replace("Gi", "GigabitEthernet").encode()
        d[f"{mibs.LLDP_REM_MAN_ADDR_IF_SUBTYPE}.0.{i}.1.1.4.{n['ip']}"] = 2
        d[f"{mibs.CDP_CACHE_DEVICE_ID}.{i}.1"] = n["name"].encode()
        d[f"{mibs.CDP_CACHE_DEVICE_PORT}.{i}.1"] = n["remote_port"].replace("Gi", "GigabitEthernet").encode()
        d[f"{mibs.CDP_CACHE_PLATFORM}.{i}.1"] = n["platform"].encode()
        d[f"{mibs.CDP_CACHE_ADDRESS}.{i}.1"] = _ip_bytes(n["ip"])
    return d, vlan_data


class SimSwitchClient(FakeSnmpClient):
    """The simulated switch with live traffic counters: each read returns the counters as they stand at
    ``clock()`` seconds, so two reads apart give rates (bandwidth, broadcast storms...)."""

    def __init__(self, data, vlan_data, ports: list[SimPort], clock=None):
        super().__init__(data, vlan_data)
        from ghostmap.collectors.portstats import COUNTERS

        self._ports = ports
        self._cols = {name: oids[0] for name, oids in COUNTERS.items()}
        start = time.monotonic()
        self.clock = clock or (lambda: time.monotonic() - start + 3600.0)

    def _now(self) -> dict:
        t = self.clock()
        d = dict(self._data)
        d[mibs.SYS_UPTIME] = int((12 * 86400 + t) * 100)
        for p in self._ports:
            for name, oid in self._cols.items():
                flow, key = p.traffic.get(name), f"{oid}.{p.if_index}"
                d[key] = int(self._data.get(key) or 0) + (int(flow.total(t)) if flow else 0)
        return d

    async def get(self, oids):
        d = self._now()
        return {oid: d.get(oid) for oid in oids}

    async def walk(self, oid, vlan=None):
        if vlan is not None:
            return await super().walk(oid, vlan)
        d = self._now()
        return sorted(((k, v) for k, v in d.items() if oid_suffix(k, oid) is not None), key=lambda kv: self._key(kv[0]))


def demo_switch_client(variant: str = "today", clock=None, which: str = "") -> SimSwitchClient:
    """``which`` "" is CELL1-SW01 on its own (the demo scan's switch); "chain" is CELL1-SW01 with CELL1-SW02
    daisy-chained off Gi1/3, and "sw02" is that second switch."""
    if which == "sw02":
        devices, ports = demo_sw02(variant)
        data, vlan_data = switch_oids(devices, ports, variant, SW02)
    else:
        devices, ports = demo_machine(variant, chain=which == "chain")
        ident = {**SW01, "neighbors": SW01["neighbors"] + [SW01_TO_SW02]}
        data, vlan_data = switch_oids(devices, ports, variant, ident)
    return SimSwitchClient(data, vlan_data, ports, clock)


def identity_reply(dev: SimDevice, context: bytes = b"\0" * 8) -> bytes:
    assert dev.cip and dev.ip
    return build_list_identity_response(ip=dev.ip, context=context, **dev.cip)


def build_demo_scan(variant: str = "today", scan_id: Optional[str] = None) -> ScanResult:
    """Run the real collector/correlation/diagnostics pipeline against the simulated cell."""
    devices, ports = demo_machine(variant)
    data, vlan_data = switch_oids(devices, ports, variant)
    t = [10.0]  # simulated seconds; the 10 s traffic window passes instantly

    async def advance(seconds: float) -> None:
        t[0] += seconds

    client = SimSwitchClient(data, vlan_data, ports, clock=lambda: t[0])
    switch = asyncio.run(sample_traffic(SWITCH_IP, client, lambda: collect_switch(SWITCH_IP, client), 10.0,
                                        sleep=advance, clock=lambda: t[0]))

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

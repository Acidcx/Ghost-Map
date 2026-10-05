import asyncio

from ghostmap.collectors.stratix import collect_switch
from ghostmap.protocols import mibs
from ghostmap.protocols.snmp import FakeSnmpClient
from ghostmap.sim.machine import SWITCH_IP, demo_machine, switch_oids


def _collect(data, vlan_data=None):
    return asyncio.run(collect_switch(SWITCH_IP, FakeSnmpClient(data, vlan_data)))


def test_demo_switch_collects_everything():
    devices, ports = demo_machine("today")
    sw = _collect(*switch_oids(devices, ports))
    assert sw.errors == []
    assert sw.sys_name == "CELL1-SW01"
    assert sw.model == "1783-BMS10CGL"
    assert sw.serial == "FOC2049X0AB"
    assert sw.software_version == "15.2(8)E4"
    assert sw.uptime_seconds == 12 * 86400

    fa12 = sw.port_by_name("Fa1/2")
    assert fa12.duplex == "half" and fa12.fcs_errors == 1532 and fa12.late_collisions == 17
    assert fa12.alias == "VFD-MAIN-CONV" and fa12.vlan == 10 and fa12.speed_mbps == 100
    assert fa12.macs == ["00:00:bc:bb:00:20"]

    assert sw.port_by_name("Fa1/5").macs == ["00:00:bc:cc:00:30", "00:00:bc:cc:00:31"]
    assert sw.port_by_name("Fa1/8").oper_status == "down"

    uplink = sw.port_by_name("Gi1/2")
    assert uplink.is_uplink and len(uplink.macs) == 24
    (n,) = uplink.neighbors  # LLDP + CDP merged into one entry
    assert n.remote_name == "PLANT-CORE.example.local"
    assert n.remote_port == "GigabitEthernet1/0/24"
    assert n.remote_platform.startswith("cisco IE-5000")
    assert n.remote_address == "10.10.0.1"

    assert sw.arp == {"192.168.1.11": "00:00:bc:aa:00:11"}


def test_qbridge_path():
    data = {
        mibs.SYS_NAME: b"sw",
        f"{mibs.IF_DESCR}.5": b"GigabitEthernet1/5",
        f"{mibs.IF_NAME}.5": b"Gi1/5",
        f"{mibs.IF_TYPE}.5": 6,
        f"{mibs.DOT1D_BASE_PORT_IFINDEX}.7": 5,
        f"{mibs.DOT1Q_TP_FDB_PORT}.10.0.0.188.1.2.3": 7,
        f"{mibs.DOT1Q_TP_FDB_STATUS}.10.0.0.188.1.2.3": 3,
        f"{mibs.DOT1Q_TP_FDB_PORT}.10.0.0.188.9.9.9": 7,
        f"{mibs.DOT1Q_TP_FDB_STATUS}.10.0.0.188.9.9.9": 4,  # self - ignored
    }
    sw = _collect(data)
    assert sw.port_by_name("Gi1/5").macs == ["00:00:bc:01:02:03"]


def test_unreachable_switch_stops_early():
    sw = _collect({})
    assert sw.errors and sw.errors[0].startswith("system:")
    assert sw.ports == []


def test_section_failure_is_recorded_not_fatal():
    devices, ports = demo_machine("today")
    data, vlan_data = switch_oids(devices, ports)

    class Flaky(FakeSnmpClient):
        async def walk(self, oid, vlan=None):
            if oid == mibs.LLDP_REM_SYS_NAME:
                raise RuntimeError("timeout")
            return await super().walk(oid, vlan)

    sw = asyncio.run(collect_switch(SWITCH_IP, Flaky(data, vlan_data)))
    assert sw.errors == ["lldp: timeout"]
    assert sw.port_by_name("Fa1/2").macs  # rest still collected

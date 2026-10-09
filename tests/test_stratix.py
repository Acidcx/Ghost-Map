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


def test_demo_switch_health():
    devices, ports = demo_machine("today")
    h = _collect(*switch_oids(devices, ports)).health
    assert (h.cpu_5s, h.cpu_1m, h.cpu_5m) == (34, 21, 17)
    assert h.memory_used == 58_000_000 and h.memory_percent == round(100 * 58 / 154, 1)  # Processor pool, not I/O
    (t,) = h.temperatures
    assert t.celsius == 51 and t.threshold == 80 and t.state == "normal"
    assert [p.state for p in h.power_supplies] == ["normal", "notFunctioning"]
    # Default context and VLAN 1 are the same instance on IOS: listed once, as VLAN 1.
    assert [(s.vlan, s.topology_changes, s.seconds_since_change) for s in h.stp] == [(1, 3, 12 * 86400), (10, 9, 420)]
    assert h.stp[0].root == "32769/00:00:5e:0a:00:01"


def test_health_from_ios_xe_and_entity_sensor_mibs():
    """No old-style CPU columns, no memory pools, no ENVMON: CPU Rev columns, cpmCPUMemory, ENTITY-SENSOR-MIB."""
    data = {
        mibs.SYS_NAME: b"sw5200",
        f"{mibs.CPM_CPU_5SEC_REV}.7": 91, f"{mibs.CPM_CPU_1MIN_REV}.7": 85, f"{mibs.CPM_CPU_5MIN_REV}.7": 82,
        f"{mibs.CPM_CPU_MEM_USED}.7": 900_000, f"{mibs.CPM_CPU_MEM_FREE}.7": 100_000,  # KB
        f"{mibs.ENT_SENSOR_TYPE}.1010": 8, f"{mibs.ENT_SENSOR_SCALE}.1010": 9, f"{mibs.ENT_SENSOR_PRECISION}.1010": 1,
        f"{mibs.ENT_SENSOR_VALUE}.1010": 456, f"{mibs.ENT_SENSOR_STATUS}.1010": 1,
        f"{mibs.ENT_PHYSICAL_NAME}.1010": b"Inlet temp",
        f"{mibs.ENT_SENSOR_TYPE}.1011": 4,  # volts: not a temperature
    }
    sw = _collect(data)
    h = sw.health
    assert sw.errors == []
    assert (h.cpu_5s, h.cpu_1m, h.cpu_5m) == (91, 85, 82)
    assert h.memory_used == 900_000 * 1024 and h.memory_percent == 90.0
    assert [(t.name, t.celsius) for t in h.temperatures] == [("Inlet temp", 45.6)]


def test_switch_without_health_mibs_reports_nothing():
    h = _collect({mibs.SYS_NAME: b"plain"}).health
    assert h.cpu_5m is None and h.memory_percent is None and not (h.temperatures or h.power_supplies or h.stp)


def test_traffic_between_wraps_and_resets():
    from ghostmap.analysis.traffic import CounterSample, traffic_between

    a = CounterSample(at=0, ports={1: {"in_octets": 2**32 - 1000, "in_bcast": 10}, 2: {"in_octets": 5}},
                      bits={"in_octets": 32, "in_bcast": 64}, speed_mbps={1: 100}, sys_uptime=100)
    b = CounterSample(at=10, ports={1: {"in_octets": 124_000, "in_bcast": 5}, 2: {"in_octets": 6}},
                      bits={"in_octets": 32, "in_bcast": 64}, speed_mbps={1: 100}, sys_uptime=1100)
    r = traffic_between(a, b)
    assert r[1].in_bps == (125_000 * 8) / 10 and r[1].in_util == 0.1  # one 32-bit wrap
    assert r[1].in_bcast_pps is None  # a 64-bit counter going backwards was cleared: no rate
    assert r[2].in_util is None  # unknown link speed
    b.sys_uptime = 50  # the switch restarted between the readings
    assert traffic_between(a, b) == {}


def test_scan_traffic_failure_is_recorded_not_fatal():
    from ghostmap.collectors.portstats import sample_traffic
    from ghostmap.collectors.stratix import collect_switch
    from ghostmap.protocols.snmp import FakeSnmpClient

    devices, ports = demo_machine("today")
    data, vlan_data = switch_oids(devices, ports)

    class NoCounters(FakeSnmpClient):
        async def walk(self, oid, vlan=None):
            if oid == mibs.IF_HC_IN_OCTETS:
                raise RuntimeError("timeout")
            return await super().walk(oid, vlan)

    client = NoCounters(data, vlan_data)
    sw = asyncio.run(sample_traffic(SWITCH_IP, client, lambda: collect_switch(SWITCH_IP, client), 10))
    assert sw.errors == ["traffic: timeout"] and sw.port_by_name("Fa1/2").macs
    assert all(p.traffic is None for p in sw.ports)

import pytest

from ghostmap.analysis.diagnostics import run_diagnostics
from ghostmap.analysis.diff import diff_scans
from ghostmap.analysis.topology import build_inventory
from ghostmap.collectors.arp import normalize_mac, parse_arp_a, parse_proc_net_arp
from ghostmap.models import DiscoveredIdentity, ScanResult, from_dict, to_dict
from ghostmap.protocols.enip import build_list_identity_response, parse_list_identity
from ghostmap.sim.machine import build_demo_scan


def ident(ip, serial, name="1734-AENTR/B", status=0x0060, state=3, code=146, rev=(6, 12), sock_ip=None):
    raw = build_list_identity_response(ip=sock_ip or ip, vendor_id=1, device_type=0x0C, product_code=code,
                                       revision=rev, status=status, serial=serial, product_name=name, state=state)
    return DiscoveredIdentity(source_ip=ip, identity=parse_list_identity(raw)[0])


def codes(findings):
    return {f.code for f in findings}


def test_duplicate_ip_and_socket_mismatch():
    ids = [ident("10.0.0.5", 1), ident("10.0.0.5", 2), ident("10.0.0.6", 3, sock_ip="192.168.0.6")]
    devices = build_inventory(ids, [], {})
    assert len(devices) == 2
    f = run_diagnostics(ids, devices, [])
    assert {"ip.duplicate", "ip.socket_mismatch"} <= codes(f)
    assert next(x for x in f if x.code == "ip.duplicate").severity == "error"


def test_baseline_and_mixed_firmware():
    ids = [ident("10.0.0.1", 1, rev=(6, 12)), ident("10.0.0.2", 2, rev=(5, 1))]
    devices = build_inventory(ids, [], {})
    f = run_diagnostics(ids, devices, [], baseline={"firmware": [{"product_name": "1734-aentr/b", "revision": "6.012"}]})
    baseline = [x for x in f if x.code == "fw.baseline"]
    assert [x.target for x in baseline] == ["1734-AENTR/B @ 10.0.0.2"]
    assert "fw.mixed" in codes(f)


def test_demo_scan_findings():
    scan = build_demo_scan("today")
    c = codes(scan.findings)
    assert {"cip.major_fault", "cip.io_faulted", "cip.minor_fault", "fw.baseline", "port.half_duplex",
            "port.errors", "port.late_collisions", "port.multi_mac", "port.down"} <= c
    assert "device.unlocated" not in c  # switch itself is placed as "self"
    by_ip = {d.ip: d for d in scan.devices}
    assert by_ip["192.168.1.20"].switch_port == "Fa1/2"
    assert by_ip["192.168.1.2"].switch_port == "self"
    assert by_ip["192.168.1.50"].identity is None and by_ip["192.168.1.50"].switch_port == "Fa1/7"
    # uplink MACs are not reported as devices
    assert not any(d.switch_port == "Gi1/2" for d in scan.devices)
    assert any(d.ip is None and d.mac == "00:1b:1b:de:ad:01" for d in scan.devices)


def test_baseline_demo_is_clean():
    scan = build_demo_scan("baseline")
    assert not [f for f in scan.findings if f.severity in ("error", "warning")]


def test_diff_detects_replacement_and_changes():
    d = diff_scans(build_demo_scan("baseline"), build_demo_scan("today"))
    assert [r["ip"] for r in d["replaced"]] == ["192.168.1.22"]
    assert d["replaced"][0]["new_firmware"] == "5.001"
    assert {a["ip"] for a in d["added"]} == {"192.168.1.50", None}
    assert d["removed"] == []
    changed = {c["ip"]: c["changes"] for c in d["changed"]}
    assert changed["192.168.1.30"]["state"]["new"] == "Major Recoverable Fault"


def test_scan_json_roundtrip():
    scan = build_demo_scan("today")
    back = from_dict(ScanResult, to_dict(scan))
    assert back == scan
    assert back.devices[1].identity.revision == scan.devices[1].identity.revision


def test_arp_parsers():
    proc = ("IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.10     0x1         0x2         00:00:bc:aa:00:10     *        eth0\n"
            "192.168.1.99     0x1         0x0         00:00:00:00:00:00     *        eth0\n")
    assert parse_proc_net_arp(proc) == {"192.168.1.10": "00:00:bc:aa:00:10"}
    windows = ("Interface: 192.168.1.5 --- 0x7\n  Internet Address      Physical Address      Type\n"
               "  192.168.1.10          00-00-bc-aa-00-10     dynamic\n"
               "  192.168.1.255         ff-ff-ff-ff-ff-ff     static\n")
    assert parse_arp_a(windows) == {"192.168.1.10": "00:00:bc:aa:00:10"}
    mac = "? (192.168.1.11) at 0:0:bc:aa:0:11 on en0 ifscope [ethernet]"
    assert parse_arp_a(mac) == {"192.168.1.11": "00:00:bc:aa:00:11"}
    assert normalize_mac("0000.bcaa.0010") == "00:00:bc:aa:00:10"
    assert normalize_mac(b"\x00\x00\xbc\xaa\x00\x10") == "00:00:bc:aa:00:10"


# ---------------------------------------------------------------- multi-switch machines

def _two_switches(neighbor_proto):
    """SW CONVEYOR (laptop + device A on Gi1/0/1) --Gi1/0/10 ... Gi1/0/9-- SW PACKER (device B on Gi1/0/1).

    Switch names deliberately contain none of the 'stratix/switch/cisco' hints.
    """
    from ghostmap.models import Neighbor, Port, SwitchInfo

    a_dev, b_dev, laptop = "00:00:bc:00:00:0a", "00:00:bc:00:00:0b", "00:50:56:00:00:99"
    a_mac, b_mac = "00:00:bc:aa:aa:01", "00:00:bc:bb:bb:01"  # switch interface MACs

    def nbr(local, peer_name, peer_ip):
        if neighbor_proto is None:
            return []
        return [Neighbor(neighbor_proto, local, remote_name=peer_name, remote_address=peer_ip)]

    conveyor = SwitchInfo(ip="10.0.0.2", sys_name="CONVEYOR", ports=[
        Port(if_index=1, name="Gi1/0/1", if_type=6, oper_status="up", mac=a_mac, macs=[a_dev, laptop]),
        Port(if_index=10, name="Gi1/0/10", if_type=6, oper_status="up", mac=a_mac,
             macs=[b_dev] + ([b_mac] if neighbor_proto is None else []),
             neighbors=nbr("Gi1/0/10", "PACKER.plant.local", "10.0.0.3")),
    ])
    packer = SwitchInfo(ip="10.0.0.3", sys_name="PACKER", ports=[
        Port(if_index=1, name="Gi1/0/1", if_type=6, oper_status="up", mac=b_mac, macs=[b_dev]),
        Port(if_index=9, name="Gi1/0/9", if_type=6, oper_status="up", mac=b_mac,
             macs=[a_dev, laptop] + ([a_mac] if neighbor_proto is None else []),
             neighbors=nbr("Gi1/0/9", "CONVEYOR", "")),
    ])
    ids = [ident("10.0.0.10", 1), ident("10.0.0.11", 2)]
    return ids, [conveyor, packer], {"10.0.0.10": a_dev, "10.0.0.11": b_dev}



@pytest.mark.parametrize("proto", ["lldp", "cdp", None])
def test_devices_located_across_multiple_switches(proto):
    ids, switches, arp = _two_switches(proto)
    devices = build_inventory(ids, switches, arp)
    where = {d.ip or d.mac: (d.switch_name, d.switch_port) for d in devices}
    assert where["10.0.0.10"] == ("CONVEYOR", "Gi1/0/1")
    assert where["10.0.0.11"] == ("PACKER", "Gi1/0/1")
    assert where["00:50:56:00:00:99"] == ("CONVEYOR", "Gi1/0/1")
    assert all(p.is_uplink for sw in switches for p in sw.ports if p.if_index != 1)
    assert not any(p.is_uplink for sw in switches for p in sw.ports if p.if_index == 1)
    # the inter-switch link must not be reported as an edge port with many MACs
    findings = run_diagnostics(ids, devices, switches)
    assert {f.target for f in findings if f.code == "port.multi_mac"} == {"CONVEYOR Gi1/0/1"}


def test_switch_health_findings():
    from ghostmap.models import Sensor, StpInfo, SwitchHealth, SwitchInfo

    def run(health, uptime=30 * 86400):
        sw = SwitchInfo(ip="10.0.0.2", sys_name="SW1", uptime_seconds=uptime, health=health)
        return {f.code: f for f in run_diagnostics([], [], [sw])}

    f = run(SwitchHealth(cpu_5s=99, cpu_1m=90, cpu_5m=85, memory_used=95, memory_free=5,
                         temperatures=[Sensor("Board", "critical", 71.0, 70.0)],
                         power_supplies=[Sensor("PS A", "normal"), Sensor("PS B", "notFunctioning"), Sensor("PS C", "notPresent")],
                         stp=[StpInfo(vlan=10, topology_changes=4, seconds_since_change=300)]))
    assert f["switch.cpu_high"].severity == "warning" and "85%" in f["switch.cpu_high"].message
    assert "switch.cpu_spike" not in f  # the sustained finding covers it
    assert f["switch.memory_high"].severity == "warning"
    assert f["switch.temperature"].severity == "error" and "71" in f["switch.temperature"].message
    assert "PS B" in f["switch.power"].message and "not working" in f["switch.power"].message  # PS C not present: no finding
    assert f["switch.stp_change"].severity == "warning" and "VLAN 10" in f["switch.stp_change"].message

    quiet = run(SwitchHealth(cpu_5s=96, cpu_1m=20, cpu_5m=12, memory_used=50, memory_free=50,
                             temperatures=[Sensor("Board", "unknown", 82.0, 80.0)],  # no state, but over its threshold
                             stp=[StpInfo(vlan=1, topology_changes=2, seconds_since_change=7200)]))
    assert set(quiet) == {"switch.cpu_spike", "switch.temperature"} and quiet["switch.cpu_spike"].severity == "info"

    # Just rebooted: the tree forming at boot is expected, so only noted.
    booted = run(SwitchHealth(stp=[StpInfo(vlan=1, topology_changes=1, seconds_since_change=600)]), uptime=700)
    assert booted["switch.stp_change"].severity == "info"


def test_demo_scan_switch_health_findings():
    c = codes(build_demo_scan("today").findings)
    assert {"switch.power", "switch.stp_change"} <= c and "switch.cpu_high" not in c


def test_demo_scan_traffic_findings():
    scan = build_demo_scan("today")
    by = {(f.code, f.target): f for f in scan.findings}
    assert by[("port.broadcast_storm", "CELL1-SW01 Fa1/6")].severity == "warning"
    assert "85% busy" in by[("port.utilization", "CELL1-SW01 Fa1/7")].message
    assert ("port.errors_rising", "CELL1-SW01 Fa1/2") in by
    sw = scan.switches[0]
    assert sw.port_by_name("Fa1/7").traffic.in_util > 80
    # survives a save and load
    again = from_dict(ScanResult, to_dict(scan))
    assert again.switches[0].port_by_name("Fa1/6").traffic.in_bcast_pps == sw.port_by_name("Fa1/6").traffic.in_bcast_pps


def test_traffic_thresholds():
    from ghostmap.analysis.diagnostics import traffic_findings
    from ghostmap.models import Port, PortTraffic

    def codes_for(**kw):
        p = Port(if_index=1, name="Fa1/1", speed_mbps=100, is_uplink=kw.pop("uplink", False),
                 traffic=PortTraffic(seconds=30, **kw))
        return {f.code: f.severity for f in traffic_findings("SW Fa1/1", p)}

    assert codes_for(in_bcast_pps=6000) == {"port.broadcast_storm": "error"}
    assert codes_for(in_bcast_pps=20, in_util=40, out_util=69) == {}
    assert codes_for(out_mcast_pps=3000) == {"port.multicast_flood": "info"}
    assert codes_for(out_mcast_pps=3000, uplink=True) == {}  # an uplink carries the VLAN's multicast
    assert codes_for(out_discards_ps=4, in_errors_ps=0.5) == {"port.drops": "warning", "port.errors_rising": "warning"}

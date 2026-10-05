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

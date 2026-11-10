"""Machine > Device health: the rules in analysis/devhealth.py and the cached tag index."""

import json
import os

from ghostmap.analysis.devhealth import device_rows, evaluate, read_list, tag_index
from ghostmap.models import CipIdentity, Device, DiscoveredIdentity, ScanResult
from ghostmap.protocols import cip_tables
from ghostmap.sim.machine import build_demo_scan
from ghostmap.web.dashboards import DashboardStore

AXIS = ["ActualPosition", "ActualVelocity", "CIPAxisState", "AxisFault", "DriveEnableStatus", "CIPAxisFaults"]


def linx_rows(axis="Ax_Feed", program=""):
    """Rows shaped like a whole-controller export from FT Linx Gateway (names made up)."""
    base = f"ns=2;s=[PLC]{'Program:' + program + '.' if program else ''}{axis}"
    rows = [[f"Online/{axis}/{m}", f"{base}.{m}", "Int16" if m == "CIPAxisState" else "Int32", 0] for m in AXIS]
    rows += [[f"Online/{axis}/{m}", f"{base}.{m}", "Float", 0] for m in
             ("MotorCapacity", "InverterCapacity", "ConverterCapacity", "BusRegulatorCapacity", "DCBusVoltage", "OutputCurrent")]
    rows += [[f"Online/{axis}/{m}", f"{base}.{m}", "Boolean", False] for m in
             ("MotorOvertemperatureFLFault", "InverterThermalOverloadULAlarm", "ThermalLimitStatus",
              "ExcessivePositionErrorFault", "ShuntThermalSwitchInputStatus")]
    return rows


CATALOG = linx_rows() + [
    ["Online/Local:2:I/Fault", "ns=2;s=::[PLC]Local:2:I.Fault", "Int32", -1],
    ["Online/Zones:3:I/Fault", "ns=2;s=::[PLC]Zones:3:I.Fault", "Int32", 0],
    ["Online/Local:2:I/Data", "ns=2;s=::[PLC]Local:2:I.Data", "Int32", 0],
    ["Online/RS_To_CTL/Connection_Status/ConnectionFaulted", "ns=2;s=[PLC]RS_To_CTL.Connection_Status.ConnectionFaulted", "Boolean", True],
    ["Diagnostic Items/@Mode", "ns=2;s=[PLC]@Mode", "String", "Remote Program"],
    ["Diagnostic Items/@ConnectionsDropped", "ns=2;s=[PLC]@ConnectionsDropped", "UInt32", 0],
    ["Diagnostic Items/@AddItems", "ns=2;s=[PLC]@AddItems", "UInt32", 5],
]


def test_tag_index_finds_axes_modules_connections_and_linx_diagnostics():
    idx = tag_index(CATALOG + linx_rows("Ax_Exit", program="Drive_System"))
    assert [a["name"] for a in idx["axes"]] == ["Ax_Exit", "Ax_Feed"]
    feed = idx["axes"][1]
    assert set(feed["members"]) >= {"MotorCapacity", "BusRegulatorCapacity", "DCBusVoltage", "CIPAxisState"}
    # thermal bits only: not position errors, not the shunt's thermal switch input
    assert set(feed["thermal"]) == {"MotorOvertemperatureFLFault", "InverterThermalOverloadULAlarm", "ThermalLimitStatus"}
    assert idx["axes"][0]["program"] == "Drive_System"
    assert [m["name"] for m in idx["modules"]] == ["Local:2", "Zones:3"]
    assert [c["name"] for c in idx["connections"]] == ["RS_To_CTL"]
    assert idx["controllers"] == [{"plc": "PLC", "items": {"@Mode": "ns=2;s=[PLC]@Mode",
                                                           "@ConnectionsDropped": "ns=2;s=[PLC]@ConnectionsDropped"}}]
    assert len(read_list(idx)) == len(set(read_list(idx)))


def test_evaluate_flags_load_heat_lost_modules_and_mode():
    idx = tag_index(CATALOG)
    nid = lambda m: f"ns=2;s=[PLC]Ax_Feed.{m}"  # noqa: E731
    values = {nid("MotorCapacity"): 85.0, nid("InverterCapacity"): 104.2, nid("ConverterCapacity"): 20.0,
              nid("CIPAxisState"): 4, nid("AxisFault"): 0, nid("InverterThermalOverloadULAlarm"): True,
              nid("MotorOvertemperatureFLFault"): False, nid("DCBusVoltage"): 640.0,
              "ns=2;s=::[PLC]Local:2:I.Fault": -1, "ns=2;s=::[PLC]Zones:3:I.Fault": 4,
              "ns=2;s=[PLC]RS_To_CTL.Connection_Status.ConnectionFaulted": True,
              "ns=2;s=[PLC]@Mode": "Remote Program", "ns=2;s=[PLC]@ConnectionsDropped": 0}
    out = evaluate(idx, values, bad=[nid("BusRegulatorCapacity")])
    d = out["drives"][0]
    codes = [i["code"] for i in d["issues"]]
    assert codes == ["drive.capacity_high", "drive.capacity_over", "drive.thermal_alarm"]
    assert d["level"] == "error" and d["state"] == "Running" and d["capacity"]["BusRegulatorCapacity"] is None
    assert d["thermal_on"] == ["Inverter Thermal Overload UL Alarm"]
    io = {r["name"]: r for r in out["io"]}
    assert io["Local:2"]["issues"][0]["code"] == "module.connection_lost"
    assert io["Zones:3"]["issues"][0]["code"] == "module.fault" and "0x00000004" in io["Zones:3"]["issues"][0]["text"]
    assert io["RS_To_CTL"]["level"] == "error"
    assert [i["code"] for i in out["controllers"][0]["issues"]] == ["controller.not_running"]
    assert out["summary"] == {"drives": 1, "drive_issues": 1, "peak_capacity": 104.2, "thermal_on": 1, "io": 3, "io_faulted": 2}
    for row in [d, *out["io"], *out["controllers"]]:
        assert all(i["hint"] and "." in i["code"] for i in row["issues"])


def test_nothing_read_is_unknown_not_ok():
    out = evaluate(tag_index(CATALOG), {}, [])
    assert {r["level"] for r in out["drives"] + out["io"] + out["controllers"]} == {"unknown"}


def test_device_rows_from_the_demo_scan():
    rows = {r["ip"]: r for r in device_rows(build_demo_scan("today"))}
    assert rows["192.168.1.30"]["level"] == "error"  # major fault on the adapter, worst first
    assert next(iter(rows)) == "192.168.1.30"
    # PowerFlex 525s have a firmware baseline: judged against it, not against each other
    assert [i["code"] for i in rows["192.168.1.22"]["issues"]] == ["fw.baseline"]
    assert rows["192.168.1.21"]["level"] == "ok"
    assert rows["192.168.1.10"]["level"] == "ok"


def _ident(ip, serial, rev=(1, 1), vendor=1, code=7, name="1734-AENTR/B"):
    i = CipIdentity(vendor, cip_tables.vendor_name(vendor), 0x0C, "Communications Adapter", code, rev[0], rev[1],
                    0x0060, serial, name, 3)
    return DiscoveredIdentity(ip, i), Device(ip=ip, identity=i)


def test_device_rows_flag_odd_firmware_and_shared_serials():
    pairs = [_ident("10.0.0.1", 0x11, (6, 12)), _ident("10.0.0.2", 0x12, (6, 12)), _ident("10.0.0.3", 0x13, (5, 1)),
             # two HMIs from one cloned image: FactoryTalk Linx answers with the same serial (seen on site)
             _ident("10.0.0.76", 0x118D5DE1, vendor=cip_tables.VENDOR_ROCKWELL_SOFTWARE, code=2, name="RSLinx Server"),
             _ident("10.0.0.77", 0x118D5DE1, vendor=cip_tables.VENDOR_ROCKWELL_SOFTWARE, code=2, name="RSLinx Server")]
    scan = ScanResult("t", "2026-11-05", identities=[p[0] for p in pairs], devices=[p[1] for p in pairs])
    rows = {r["ip"]: r for r in device_rows(scan)}
    assert [i["code"] for i in rows["10.0.0.3"]["issues"]] == ["fw.mismatch"]
    assert "6.012 on 2 devices" in rows["10.0.0.3"]["issues"][0]["text"]
    assert rows["10.0.0.1"]["level"] == "ok"
    linx = rows["10.0.0.76"]["issues"][0]
    assert linx["code"] == "device.shared_serial" and linx["severity"] == "error" and "10.0.0.77" in linx["text"]


def test_health_index_is_built_once_per_tag_export(tmp_path, monkeypatch):
    store = DashboardStore(tmp_path)
    store.dir.mkdir()
    p = store.dir / "m-1.tags.json"
    p.write_text(json.dumps(CATALOG))
    calls = []
    real = store.load_tags
    monkeypatch.setattr(store, "load_tags", lambda did: calls.append(did) or real(did))
    a = store.health_index("m-1")
    assert store.health_index("m-1") is a and calls == ["m-1"]  # a page refresh doesn't parse the export again
    p.write_text(json.dumps(CATALOG[len(AXIS):]))
    os.utime(p, ns=(1, 1))
    assert store.health_index("m-1")["axes"] == [] and len(calls) == 2
    assert store.health_index("missing")["axes"] == []

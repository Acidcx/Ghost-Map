"""Drive and device health: what Ghost Map can already read about each drive, I/O module, controller and
network device, turned into rows with issues (dotted ``code``, ``hint``) for Machine > Device health.

Two sources, both read-only:

* **The machine's PLC tags through FT Linx Gateway (OPC UA).** ``tag_index`` finds, in a dashboard's stored tag
  export, the members worth reading: each CIP Motion axis's thermal model (Motor / Inverter / Converter / Bus
  regulator capacity, % of rated), DC bus voltage, output current and power, state and fault words, and its
  over-temperature and thermal-overload alarm and fault bits; each I/O module's ``:I.Fault`` word; produced /
  consumed tags' ``ConnectionFaulted`` bit; and FT Linx's own Diagnostic Items per controller (mode, revision,
  connection drops). ``evaluate`` turns one read of those into rows. Kinetix drives don't publish a temperature
  over the network: the capacity numbers are their thermal model, and 100 % is the trip point.
* **The last network scan** (ListIdentity): ``device_rows`` gives each device its CIP status (faults, I/O
  connections), firmware against the other devices of the same product, and a serial that answers from more
  than one address (a cloned PC image, or two devices with one identity).

Pure functions only; no network access.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from ghostmap.analysis.dashboard import AXIS_SIGNATURE, humanize
from ghostmap.models import ScanResult
from ghostmap.protocols import cip_tables

# Thermal model, % of rated. Kinetix folds back or trips at 100-110 %.
CAPACITY_WARN = 80.0
CAPACITY_ERROR = 100.0
CAPACITIES = [("MotorCapacity", "Motor"), ("InverterCapacity", "Inverter"), ("ConverterCapacity", "Converter"),
              ("BusRegulatorCapacity", "Bus regulator")]
VALUES = ["DCBusVoltage", "OutputCurrent", "OutputPower", "OutputFrequency"]
STATE = ["CIPAxisState", "AxisFault", "CIPAxisAlarms"]
# Over-temperature and overload bits of an AXIS_CIP_DRIVE (FL = factory limit, UL = user limit).
_THERMAL_BIT = re.compile(r"(overtemp|thermal|overload)\w*(fault|alarm)$|^thermallimitstatus$|^smtovertemperaturestatus$", re.I)
_MODULE_FAULT = re.compile(r"^(?P<mod>[^.\[\]]+?):I\.Fault$")
_CONN_FAULTED = re.compile(r"^(?P<tag>.+)\.Connection_Status\.ConnectionFaulted$")
_SHORTCUT = re.compile(r"^(?:ns=\d+;s=)?(?:::)?\[(?P<plc>[^\]]+)\](?P<rest>.*)$")
DIAG_ITEMS = ["@Mode", "@Revision", "@Status", "@ConnectionsDropped", "@LastConnectionDropTime",
              "@AveragePacketResponse", "@CLXPresentScanTime", "@CLXMaxScanTime"]
AXIS_STATES = ["Initializing", "Pre-charge", "Stopped", "Starting", "Running", "Testing", "Stopping",
               "Aborting", "Major faulted", "Start inhibited", "Shutdown"]
SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2, "ok": 3, "unknown": 4}
MAX_TAGS = 4000  # one health read stays well under this; a controller with hundreds of axes is cut here


def _split_nid(nid: str) -> tuple[str, str]:
    """``ns=2;s=::[PLC]Local:2:I.Fault`` -> ``("PLC", "Local:2:I.Fault")``."""
    m = _SHORTCUT.match(nid)
    if m:
        return m["plc"], m["rest"]
    return "", nid.split(";s=", 1)[-1]


def _issue(severity: str, code: str, text: str, hint: str) -> dict:
    return {"severity": severity, "code": code, "text": text, "hint": hint}


def _level(issues: list[dict], known: bool = True) -> str:
    if not known:
        return "unknown"
    return min((i["severity"] for i in issues), key=SEVERITY_RANK.__getitem__, default="ok")


# ------------------------------------------------------------------------------------------- tag index
def tag_index(catalog: Iterable[list]) -> dict:
    """What a dashboard's stored tag export (``[path, node_id, type, value]`` rows) offers for the health page.

    Built once per export and kept by the caller: a whole-controller export is ~200,000 rows.
    """
    groups: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    modules, conns, diag = [], [], defaultdict(dict)
    for row in catalog:
        nid, typ = str(row[1]), str(row[2] or "")
        plc, rest = _split_nid(nid)
        leaf = rest.rsplit(".", 1)[-1]
        if "." in rest:
            groups[nid.rsplit(".", 1)[0]][leaf] = (nid, typ)
        if leaf in DIAG_ITEMS or rest in DIAG_ITEMS:
            diag[plc][rest.rsplit(".", 1)[-1]] = nid
            continue
        m = _MODULE_FAULT.match(rest)
        if m and typ not in ("Boolean", "String"):
            modules.append({"name": m["mod"], "plc": plc, "node_id": nid})
            continue
        m = _CONN_FAULTED.match(rest)
        if m:
            conns.append({"name": m["tag"].replace("Program:", ""), "plc": plc, "node_id": nid})
    axes = []
    for base, mem in groups.items():
        if len(AXIS_SIGNATURE & set(mem)) < 4:
            continue
        plc, rest = _split_nid(base)
        name = rest.rsplit(".", 1)[-1]
        wanted = {k: mem[k][0] for k in [c for c, _ in CAPACITIES] + VALUES + STATE if k in mem}
        thermal = {k: nid for k, (nid, typ) in mem.items() if typ == "Boolean" and _THERMAL_BIT.search(k)}
        axes.append({"name": name, "label": humanize(name), "plc": plc, "base": base,
                     "program": rest.split(".", 1)[0].replace("Program:", "") if rest.startswith("Program:") else "",
                     "members": wanted, "thermal": thermal,
                     # every direct member, for an axis's Details panel (Machine > Drives)
                     "all": {k: v for k, v in mem.items()}})
    axes.sort(key=lambda a: (a["plc"], a["name"].lower()))
    modules.sort(key=lambda m: (m["plc"], _natural(m["name"])))
    conns.sort(key=lambda c: (c["plc"], c["name"].lower()))
    return {"axes": axes, "modules": modules, "connections": conns,
            "controllers": [{"plc": plc, "items": items} for plc, items in sorted(diag.items())]}


def _natural(s: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def read_list(index: dict) -> list[str]:
    """Every NodeId one health read takes, at most ``MAX_TAGS``."""
    ids: dict[str, None] = {}
    for a in index["axes"]:
        ids.update(dict.fromkeys(a["members"].values()))
        ids.update(dict.fromkeys(a["thermal"].values()))
    for m in index["modules"] + index["connections"]:
        ids[m["node_id"]] = None
    for c in index["controllers"]:
        ids.update(dict.fromkeys(c["items"].values()))
    return list(ids)[:MAX_TAGS]


# ------------------------------------------------------------------------------------------- evaluate
def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def drive_row(axis: dict, values: dict, bad: set) -> dict:
    """One axis: its thermal model, power values, state and the thermal bits that are on."""
    def val(nid: Optional[str]):
        return None if nid is None or nid in bad or nid not in values else values[nid]

    m = axis["members"]
    known = any(nid in values and nid not in bad for nid in [*m.values(), *axis["thermal"].values()])
    issues: list[dict] = []
    caps = {}
    for key, label in CAPACITIES:
        if key not in m:
            continue
        pct = _num(val(m[key]))
        caps[key] = pct
        if pct is None:
            continue
        if pct >= CAPACITY_ERROR:
            issues.append(_issue("error", "drive.capacity_over", f"{label} at {pct:.0f} % of its rating.",
                                 f"At or past the {label.lower()}'s thermal limit: the drive folds back or trips. "
                                 "Look for mechanical drag, binding, a dragging brake or a worn bearing, and compare the load with other shifts."))
        elif pct >= CAPACITY_WARN:
            issues.append(_issue("warning", "drive.capacity_high", f"{label} at {pct:.0f} % of its rating.",
                                 f"Running hot: more than {CAPACITY_WARN:.0f} % of the {label.lower()}'s thermal rating. "
                                 "Check for drag or binding, cooling fans and cabinet temperature before it reaches 100 %."))
    state = val(m.get("CIPAxisState"))
    state_n = int(state) if _num(state) is not None else None
    state_name = AXIS_STATES[state_n] if state_n is not None and 0 <= state_n < len(AXIS_STATES) else ("" if state_n is None else str(state_n))
    fault = _num(val(m.get("AxisFault")))
    if state_n == 8 or (fault or 0) != 0:
        issues.append(_issue("error", "drive.faulted", f"Axis {state_name.lower() or 'faulted'}" + (f", AxisFault = {int(fault)}" if fault else "") + ".",
                             "Open Machine > Dashboard > Drives > Details for the fault bits, or the drive's fault log in Studio 5000."))
    alarms = _num(val(m.get("CIPAxisAlarms")))
    if alarms:
        issues.append(_issue("warning", "drive.alarm", f"Axis alarm word is {int(alarms)}.",
                             "An alarm comes before a fault: Details on Machine > Dashboard > Drives names the bit."))
    hot = []
    for name, nid in sorted(axis["thermal"].items()):
        if val(nid) is True:
            hot.append(name)
            sev = "error" if name.endswith("Fault") else "warning"
            issues.append(_issue(sev, "drive.thermal_fault" if sev == "error" else "drive.thermal_alarm", f"{humanize(name)}.",
                                 "Over temperature or overload in the drive or motor: check cabinet cooling and fans, the motor's "
                                 "fan and dirt on it, and the load. FL = the drive's factory limit, UL = the user limit set in the axis."))
    return {"name": axis["name"], "label": axis["label"], "plc": axis["plc"], "program": axis["program"],
            "base": axis["base"], "state": state_name, "capacity": caps,
            "values": {k: _num(val(m[k])) for k in VALUES if k in m},
            "thermal_checked": len(axis["thermal"]), "thermal_on": [humanize(n) for n in hot],
            "issues": issues, "level": _level(issues, known)}


def module_row(mod: dict, values: dict, bad: set) -> dict:
    nid = mod["node_id"]
    known = nid in values and nid not in bad
    v = _num(values.get(nid)) if known else None
    issues = []
    if v is not None and v != 0:
        word = int(v) & 0xFFFFFFFF
        if word == 0xFFFFFFFF:
            issues.append(_issue("error", "module.connection_lost", "No connection to the module (fault word all ones).",
                                 "The controller can't reach this module: check its power, network cable and status LED, "
                                 "and that it is not inhibited in the I/O tree."))
        else:
            issues.append(_issue("warning", "module.fault", f"Fault word 0x{word:08X}.",
                                 "One or more channels or the module report a fault: open the module's properties in Studio 5000."))
    return {"name": mod["name"], "plc": mod["plc"], "kind": "I/O module", "value": None if v is None else int(v),
            "issues": issues, "level": _level(issues, known)}


def connection_row(conn: dict, values: dict, bad: set) -> dict:
    nid = conn["node_id"]
    known = nid in values and nid not in bad
    issues = []
    if known and values.get(nid) is True:
        issues.append(_issue("error", "connection.faulted", "Produced/consumed connection faulted.",
                             "The controller isn't getting this tag from the other controller: check that it is in Run, "
                             "the network path between them, and the RPI and tag names on both sides."))
    return {"name": conn["name"], "plc": conn["plc"], "kind": "Produced/consumed tag",
            "value": values.get(nid) if known else None, "issues": issues, "level": _level(issues, known)}


def controller_row(ctl: dict, values: dict, bad: set) -> dict:
    items = ctl["items"]
    got = {k.lstrip("@"): values.get(nid) for k, nid in items.items() if nid in values and nid not in bad}
    issues = []
    mode = str(got.get("Mode") or "")
    if mode and "fault" in mode.lower():
        issues.append(_issue("error", "controller.faulted", f"Controller mode: {mode}.",
                             "The controller has a major fault: read the fault code in Studio 5000 (Controller Properties > Major Faults)."))
    elif mode and "run" not in mode.lower():
        issues.append(_issue("warning", "controller.not_running", f"Controller mode: {mode}.",
                             "The controller is not in Run: check the key switch and who is online with Studio 5000."))
    status = str(got.get("Status") or "")
    if status and status.upper() != "OK":
        issues.append(_issue("warning", "gateway.status", f"FT Linx reports the controller as {status}.",
                             "FT Linx can't read the controller normally: check the path in the Linx shortcut and the network to the controller."))
    drops = _num(got.get("ConnectionsDropped"))
    if drops:
        last = got.get("LastConnectionDropTime") or ""
        issues.append(_issue("info", "gateway.drops", f"FT Linx dropped its connection {int(drops)} times" + (f", last at {last}" if last else "") + ".",
                             "Counts since Linx started. Rising counts point at the network path to the controller or a busy controller."))
    return {"plc": ctl["plc"], "items": got, "issues": issues, "level": _level(issues, bool(got))}


def evaluate(index: dict, values: dict, bad: Iterable[str]) -> dict:
    """One health read (``values`` by NodeId; ``bad`` = tags that came back with a Bad status) as rows."""
    b = set(bad)
    drives = [drive_row(a, values, b) for a in index["axes"]]
    io = [module_row(m, values, b) for m in index["modules"]] + [connection_row(c, values, b) for c in index["connections"]]
    controllers = [controller_row(c, values, b) for c in index["controllers"]]
    caps = [v for d in drives for v in d["capacity"].values() if v is not None]
    return {"drives": drives, "io": io, "controllers": controllers,
            "summary": {"drives": len(drives), "drive_issues": sum(d["level"] in ("error", "warning") for d in drives),
                        "peak_capacity": max(caps) if caps else None,
                        "thermal_on": sum(len(d["thermal_on"]) for d in drives),
                        "io": len(io), "io_faulted": sum(r["level"] == "error" for r in io)}}


# ------------------------------------------------------------------------------------------- network devices
def device_rows(scan: ScanResult) -> list[dict]:
    """Every EtherNet/IP device in a scan with its health from ListIdentity, worst first."""
    by_product: dict[tuple[int, int], Counter] = defaultdict(Counter)
    for d in scan.devices:
        i = d.identity
        if i and i.vendor_id != cip_tables.VENDOR_ROCKWELL_SOFTWARE:
            by_product[(i.vendor_id, i.product_code)][i.revision] += 1
    serial_ips: dict[tuple[int, int], set[str]] = defaultdict(set)
    for di in scan.identities:
        if di.identity.serial:
            serial_ips[(di.identity.vendor_id, di.identity.serial)].add(di.source_ip)
    findings: dict[str, list] = defaultdict(list)
    for f in scan.findings:
        findings[f.target].append(f)

    # A product with a firmware baseline is judged against the baseline (fw.baseline), not against its neighbours.
    baselined = {(d.identity.vendor_id, d.identity.product_code) for d in scan.devices if d.identity
                 and any(f.code == "fw.baseline" for f in findings.get(d.label, []))}
    rows = []
    for d in scan.devices:
        i = d.identity
        if i is None:
            continue
        issues = []
        for f in findings.get(d.label, []) + (findings.get(d.ip, []) if d.ip else []):
            if f.severity in ("error", "warning") or f.code.startswith("cip."):
                issues.append(_issue(f.severity, f.code, f.message, f.hint))
        revs = by_product.get((i.vendor_id, i.product_code))
        if revs and len(revs) > 1 and (i.vendor_id, i.product_code) not in baselined:
            common, n = revs.most_common(1)[0]
            tie = sum(1 for c in revs.values() if c == n) > 1
            if tie or i.revision != common:
                others = ", ".join(f"{r} on {c} device{'s' if c > 1 else ''}" for r, c in sorted(revs.items()) if r != i.revision)
                issues.append(_issue("warning", "fw.mismatch", f"Firmware {i.revision}; other {i.product_name} here: {others}.",
                                     "Mixed firmware on identical devices: a replaced spare that was never flashed is the usual cause. "
                                     "Check against the machine's standard and update with ControlFLASH at a planned stop."))
        others = sorted(serial_ips.get((i.vendor_id, i.serial), set()) - {d.ip})
        if others:
            linx = i.vendor_id == cip_tables.VENDOR_ROCKWELL_SOFTWARE
            issues.append(_issue("error" if linx else "warning", "device.shared_serial",
                                 f"Serial {i.serial_hex} also answers from {', '.join(others)}.",
                                 "Two PCs from one cloned image share FactoryTalk Linx's serial, which confuses controllers and "
                                 "Ghost Map: reinstall Linx on one of them at a planned stop (sysprep future images)." if linx else
                                 "Two addresses with one identity: normal for a dual-IP module, otherwise a cloned or "
                                 "misconfigured device. Check both addresses with Probe."))
        flags = [f.replace("_", " ") for f in i.status_flags]
        rows.append({"ip": d.ip, "mac": d.mac, "product": i.product_name, "vendor": i.vendor_name, "type": i.device_type_name,
                     "revision": i.revision, "serial": i.serial_hex, "state": i.state_name, "status": i.extended_status,
                     "flags": flags, "port": f"{d.switch_name or d.switch_ip} {d.switch_port}" if d.switch_port else "",
                     "issues": issues, "level": _level(issues)})
    rows.sort(key=lambda r: (SEVERITY_RANK[r["level"]], [int(p) for p in (r["ip"] or "999.0.0.0").split(".")]))
    return rows

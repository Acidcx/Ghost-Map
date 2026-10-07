"""Build a machine dashboard layout from a tag export.

Input is the rows of a Tag Browser export (``path``, ``node_id``, ``type`` or
``variant_type``, ``value``). Output is a layout: areas (one per folder of the
export), each with alarms, timers, counters, fault words, values and status
bits, plus hints where the generator is unsure.

Tag names drift from machine to machine, so the generator leans on structure
first (Logix TIMER / COUNTER members, data types, which folder a tag sits in)
and on names second (``_Warn``, ``E_Stop``, ``_MS``, ``Comms_Flt``...). Every
guess can be overridden per dashboard: invert an area, hide a tag, rename.

Pure functions only; no network access.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Iterable, Optional

TIMER_MEMBERS = {"EN", "TT", "DN", "ACC", "PRE"}
COUNTER_MEMBERS = {"CU", "CD", "DN", "OV", "UN", "ACC", "PRE"}
BOOL_TYPES = {"Boolean"}
INT_TYPES = {"SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"}
REAL_TYPES = {"Float", "Double"}

# Logix AXIS_CIP_DRIVE (and similar) structures: recognised by their members, shown as one axis
# with its state, a few values and fault words instead of ~600 separate tags.
AXIS_SIGNATURE = {"ActualPosition", "ActualVelocity", "CIPAxisState", "AxisFault", "DriveEnableStatus", "CIPAxisFaults"}
AXIS_KEYS = ["CIPAxisState", "DriveEnableStatus", "ServoActionStatus", "AxisHomedStatus", "ActualPosition",
             "ActualVelocity", "CurrentFeedback", "DCBusVoltage", "MotorCapacity", "InverterCapacity",
             "AxisFault", "CIPAxisFaults", "CIPAxisAlarms", "ModuleFaults", "GuardFaults", "MotionFaultStatus",
             "CIPInitializationFaults", "CIPAPRFaults", "AxisSafetyFaults", "CIPStartInhibits"]
AXES_AREA = "Motion axes"
# What an axis's Details panel shows, read once on request from the axis's ~600 members.
AXIS_DETAIL = {
    "Motion": ["CommandPosition", "ActualPosition", "PositionError", "CommandVelocity", "ActualVelocity",
               "VelocityError", "ActualAcceleration", "CommandTorque", "TorqueReference"],
    "Power": ["DCBusVoltage", "OutputCurrent", "OutputVoltage", "OutputFrequency", "OutputPower", "CurrentFeedback",
              "MotorCapacity", "InverterCapacity", "ConverterCapacity", "BusRegulatorCapacity"],
    "Limits": ["TorqueLimitPositive", "TorqueLimitNegative", "OperativeCurrentLimit", "CurrentLimitSource",
               "VelocityLimitSource"],
    "Tuning": ["SystemInertia", "PositionLoopBandwidth", "VelocityLoopBandwidth", "VelocityIntegratorBandwidth",
               "TorqueLowPassFilterBandwidth"],
    "Fault words": ["AxisFault", "CIPAxisFaults", "CIPAxisAlarms", "ModuleFaults", "GuardFaults",
                    "CIPInitializationFaults", "CIPStartInhibits", "CIPAPRFaults", "AxisSafetyFaults",
                    "MotionFaultStatus", "AttributeErrorCode", "AttributeErrorID"],
}
INSTRUCTION_MEMBERS = {"EN", "DN", "ER"}

# Keep live reads small: per area at most this many status bits, values and counters (alarms are all kept).
MAX_PER_AREA = 12
DEDUPE_MIN_TAGS = 8       # only structures this big are checked for program-parameter copies
MAX_ARRAY_INDEX = 63      # array elements past this are skipped (recipe tables, data logs)
_MODULE_TAG = re.compile(r"^[^:/]+:(\d+:)?[IOCS]\d*$|^Local:", re.I)  # I/O module tags like Rack1:3:I
_ARRAY_IDX = re.compile(r"\[(\d+)(?:,\d+)*\]")
# Outside fault folders only these status bits and values go on the dashboard by default; the rest can be
# added with Edit. A whole controller has tens of thousands of bits and REALs that mean nothing on their own.
_RELEVANT_STATUS = re.compile(r"(run|auto|manual|mode|ready|enable|homed|state|cycle|start|stop|idle|hold|"
                              r"jog|thread|batch|in_?pos|complete|done|active|healthy|ok$)", re.I)
_NOT_STATUS = re.compile(r"(runout|rundown|runtime|run_?time|rung|_cmd$|_pb$|^hmi_|req$|request)", re.I)
_RELEVANT_VALUE = re.compile(r"(speed|fpm|rpm|length|feet|_ft$|count|cnt|total|temp|pressure|psi|current|amps?$|"
                             r"torque|load|thick|width|gauge|weight|cycle|rate|percent|pct)", re.I)
_RUNNING = [(re.compile(r"^(machine_?|line_?|mach_?)?running$|^runf$|^run_?fb$|^line_?run$|^mach_?run$", re.I), 3),
            (re.compile(r"^run$", re.I), 2),
            (re.compile(r"(^|_)running($|_)|autorun|auto_?running|in_?auto_?run", re.I), 2),
            (re.compile(r"(^|_)run(ning)?($|_)", re.I), 1)]
# A tag the PLC changes all the time (a heartbeat counter, a watchdog), so frozen data can be told apart from
# a quiet machine. Counters beat toggling BOOLs: a bit that flips every second can look frozen to a 2 s poll.
_HEARTBEAT = re.compile(r"heart_?beat|watch_?dog|(^|_)hb($|_)|life_?(bit|sign|count)|live_?count|alive|wall_?clock", re.I)
# One-shot storage bits (OSg.CL1.Start, OS1.x, ONS): rung internals that change for one scan.
_ONESHOT = re.compile(r"^(os|osg|ons|osr|osf|one_?shots?)\d*$", re.I)
# Bits in a fault folder that are commands, not conditions (Reset_Faults, Fault_Ack, HMI_Clear_PB).
_COMMAND = re.compile(r"(^|_)(reset|rst|clear|clr|ack|acknowledge|silence)(_|$)|_cmd$|_pb$|^hmi_|req$|request", re.I)
# Alarm-folder bits that are on when healthy (No_Faults, Comms_OK): active when off.
_OK_WHEN_ON = re.compile(r"^(no|not)_?(faults?|flts?|alarms?|alms?|errors?|errs?)$|(^|_)(ok|healthy|good)$", re.I)
FRESHNESS = ("heartbeat", "response")
DEFAULT_MAX_READ_MS = 2000

# An area where at least this share of the bits is on, and whose bit names
# don't say "fault", may be using "on = healthy" (e.g. comms OK bits).
INVERT_SHARE = 0.6
INVERT_MIN_BITS = 4

# "Dflt" is "default", not a fault.
_FAULT_CONTEXT = re.compile(r"(fault|(?<!d)flt|alarm|alm|error|err\b|warn)", re.I)
_FAULT_WORD = re.compile(r"(fault|(?<!d)flt|alarm|alm|error|warn)", re.I)
_AOI_INTERNAL = {"EnableIn", "EnableOut"}  # every Add-On Instruction instance has these
_SPARE = re.compile(r"(^|[_./\[])spare", re.I)
_COUNT_NAME = re.compile(r"(count|cnt|ctr|strokes?|fires?|cycles?|parts?)", re.I)

# (category, pattern) in priority order; matched against area + name.
CATEGORIES = [
    ("estop", re.compile(r"(e_?stop|emer)", re.I)),
    ("comms", re.compile(r"(comm|aent|rem_mod|_rem_|io_flt|lost)", re.I)),
    ("guard", re.compile(r"(guard|safety|light_?curtain|gate)", re.I)),
    ("motor", re.compile(r"(_ms($|_)|motor_starter|overload|_ol($|_))", re.I)),
    ("temperature", re.compile(r"(temp|_ot$|_ot_|overheat)", re.I)),
    ("hydraulic", re.compile(r"(hpu|hyd|(^|_)oil|filter|(^|_)(no_)?flow)", re.I)),
    ("drive", re.compile(r"(vfd|drive|sercos|motion|(^|_)enc|servo|axis|gear)", re.I)),
    ("power", re.compile(r"(volt|power|battery|phase|_l[123]$)", re.I)),
    ("air", re.compile(r"(air)", re.I)),
]

CATEGORY_TITLES = {
    "estop": "E-stop", "comms": "Comms", "guard": "Guards and safety", "motor": "Motor starters",
    "temperature": "Temperature", "hydraulic": "Hydraulics", "drive": "Drives and motion",
    "power": "Power", "air": "Air", "other": "Other",
}

_WORDS = {
    "flt": "Fault", "flts": "Faults", "sts": "Status", "tmr": "Timer", "ms": "Motor Starter",
    "ot": "Over Temp", "overtemp": "Over Temp", "comm": "Comms", "comms": "Comms", "pres": "Pressure",
    "matl": "Material", "enc": "Encoder", "hyd": "Hydraulic", "warn": "Warning", "cnt": "Count",
    "vel": "Velocity", "mod": "Module", "rem": "Remote", "estop": "E-Stop", "emer": "Emergency",
}


def humanize(name: str) -> str:
    """``EN_Motor_OverTemp_Warn`` -> ``EN Motor Over Temp Warning``. The raw name stays as a tooltip.

    CamelCase is split too: ``BusUndervoltageFault`` -> ``Bus Undervoltage Fault``.
    """
    out = []
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name) if "_" not in name else name
    for part in re.split(r"[_.]+", name):
        if not part:
            continue
        if part.lower() == "e" and not out:
            out.append("E-")
            continue
        out.append(_WORDS.get(part.lower(), part))
    text = " ".join(out).replace("E- Stop", "E-Stop")
    return text


def tag_path(node_id: str) -> list[str]:
    """Segments of a Logix-style NodeId, without namespace, shortcut and program prefix.

    ``ns=2;s=::[PLC]Program:MainProgram.FAULT.Zone1.E_Stop`` -> ``["FAULT", "Zone1", "E_Stop"]``
    """
    s = node_id.split(";s=", 1)[1] if ";s=" in node_id else node_id
    s = re.sub(r"^(::)?\[[^\]]*\]", "", s)
    s = re.sub(r"^Program:[^.]+\.", "", s)
    return [p for p in s.split(".") if p]


def _type(row: dict) -> str:
    return row.get("type") or row.get("variant_type") or ""


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1")
    return bool(v)


def category(area: str, name: str) -> str:
    folder = area.split("/")[-1]
    if re.search(r"comm", folder, re.I):  # everything in a Comms folder is a comms bit
        return "comms"
    text = f"{area}/{name}"
    for cat, rx in CATEGORIES:
        if rx.search(name):
            return cat
    for cat, rx in CATEGORIES:  # area name as a fallback ("Comms/VFD_01")
        if rx.search(text):
            return cat
    return "other"


def severity(name: str, cat: str) -> str:
    n = name.lower()
    if re.search(r"(warn|warning)$|_warn_|_wrn$", n):
        return "warning"
    if re.search(r"(_sts|_status)$", n):
        return "status"
    if cat == "estop":
        return "critical"
    return "fault"


def _split(row: dict) -> tuple[list[str], str]:
    """(area segments, tag name) for an export row."""
    parts = [p for p in str(row.get("path", "")).split("/") if p]
    if len(parts) > 1:
        return parts[:-1], parts[-1]
    segs = tag_path(row.get("node_id", ""))
    name = parts[0] if parts else (segs[-1] if segs else row.get("node_id", "?"))
    return (segs[-2:-1] or ["Tags"]), name


def _titles(areas: list[dict]) -> None:
    """Short area titles: the folder name when it's unique within its section, else the path without the
    program segment (the section shows that) and without a root folder every area shares (FT Linx "Online")."""
    paths = [a["id"].split("/") for a in areas]
    root = paths[0][0] if paths and all(len(p) > 1 and p[0] == paths[0][0] for p in paths) else None

    def trimmed(p):
        p = p[1:] if root else p
        return [s for s in p if not s.lower().startswith("program:")] or [_section("/".join(p))]
    for a, p in zip(areas, paths):
        a["_t"] = trimmed(p)
    for a in areas:
        same = [b for b in areas if b["_t"][-1] == a["_t"][-1] and _section(b["id"]) == _section(a["id"])]
        a["title"] = a["_t"][-1] if len(same) == 1 else " / ".join(a["_t"])
    for a in areas:
        del a["_t"]


def _excluded(row: dict) -> bool:
    """I/O module tags, arrays outside fault folders, big arrays: thousands of tags that don't belong on a
    dashboard (recipe tables, queues, data logs)."""
    text = f"{row.get('path', '')}/{'/'.join(tag_path(row.get('node_id', '')))}"
    for seg in re.split(r"[/.]", text):
        if _MODULE_TAG.match(seg) or _ONESHOT.match(seg):
            return True
    idx = [int(m.group(1)) for m in _ARRAY_IDX.finditer(text)]
    return bool(idx) and (max(idx) > MAX_ARRAY_INDEX or not _FAULT_CONTEXT.search(text))


def _dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop program-scope copies of controller-scope structures.

    FT Linx Gateway shows a program's InOut parameters and aliases as tags of their own, so one UDT can show
    up ten times (``OSg`` and ``Program:X/OS`` in every program). Two subtrees are the same tag when their
    member names, types and BOOL values all match. Only program-scope copies of a controller-scope tag are
    dropped; look-alike tags that are all program-local (two programs' own Matl_Props) are kept.
    """
    import hashlib
    from collections import defaultdict

    kids: dict = defaultdict(dict)
    size: dict = defaultdict(int)
    for r in rows:
        parts = r["path"].split("/")
        for i in range(1, len(parts)):
            size["/".join(parts[:i])] += 1
        t = _type(r)
        kids["/".join(parts[:-1])][parts[-1]] = t + ("=" + str(_truthy(r.get("value"))) if t in BOOL_TYPES else "")
    groups: dict = defaultdict(list)
    for d in sorted(kids, key=lambda p: -p.count("/")):
        h = hashlib.sha1(repr(sorted(kids[d].items())).encode()).hexdigest()
        if d:
            parent, _, leaf = d.rpartition("/")
            kids[parent][leaf] = h
        if size[d] >= DEDUPE_MIN_TAGS:
            groups[h].append(d)
    scoped = lambda d: any(seg.lower().startswith("program:") for seg in d.split("/"))  # noqa: E731
    drop = set()
    for members in groups.values():
        if len(members) > 1 and any(not scoped(d) for d in members):
            drop.update(d for d in members if scoped(d))
    if not drop:
        return rows, 0

    def dropped(path):
        parts = path.split("/")
        return any("/".join(parts[:i]) in drop for i in range(1, len(parts)))
    kept = [r for r in rows if not dropped(r["path"])]
    return kept, len(rows) - len(kept)


def _section(area_id: str) -> str:
    """Programs become sections; controller-scope tags go under "Controller"."""
    for seg in area_id.split("/"):
        if seg.lower().startswith("program:"):
            return seg.split(":", 1)[1]
    return "Controller"


def _running_score(name: str) -> int:
    if _NOT_STATUS.search(name):
        return 0
    return next((score for rx, score in _RUNNING if rx.search(name)), 0)


def _heartbeat_guess(rows: list[dict]) -> list[str]:
    best = None
    for r in rows:
        leaf = str(r.get("path") or r["node_id"]).split("/")[-1].split(".")[-1]
        t = _type(r)
        if (not _HEARTBEAT.search(leaf) or _FAULT_WORD.search(leaf) or _excluded(r)
                or t not in BOOL_TYPES | INT_TYPES | REAL_TYPES):
            continue
        stored = bool(re.search(r"(mem|last|prev|old|copy|save[d]?)$", leaf, re.I))  # a copy, not the live value
        key = (t in BOOL_TYPES, stored, "program:" in r["node_id"].lower(), len(r["node_id"]))
        if best is None or key < best[0]:
            best = (key, r["node_id"])
    return [best[1]] if best else []


def build_layout(rows: Iterable[dict], name: str = "Machine") -> dict:
    rows = [r for r in rows if r.get("node_id")]
    total = len(rows)
    # FT Linx diagnostics (@...), strings and their SINT characters aren't machine data.
    rows = [r for r in rows if not _excluded(r) and _type(r) not in ("SByte", "Byte", "String")
            and not str(r.get("path", "")).split("/")[-1].startswith("@")
            and str(r.get("path", "")).split("/")[-1] not in _AOI_INTERNAL]
    excluded = total - len(rows)
    rows, duplicates = _dedupe(rows)
    heartbeat = _heartbeat_guess(rows)
    # FT Linx puts everything under "Online"; a folder every tag shares adds nothing to area names.
    firsts = {str(r.get("path", "")).split("/")[0] for r in rows}
    if len(firsts) == 1 and all("/" in str(r.get("path", "")) for r in rows) and len(rows) > 1:
        rows = [dict(r, path=r["path"].split("/", 1)[1]) for r in rows]

    # 0. Motion axes: a parent whose members look like an AXIS_CIP_DRIVE.
    groups: dict[tuple, dict] = {}
    for r in rows:
        segs, leaf = _split(r)
        groups.setdefault(tuple(segs), {})[leaf] = r
    axes = {k: g for k, g in groups.items() if len(AXIS_SIGNATURE & set(g)) >= 4}
    # Instruction tags (MSG, MAFR/MSO and other MOTION_INSTRUCTION, CONTROL): EN/DN/ER handshake bits that
    # are internal to the logic. Leave them out rather than show "ER" as ten alarms per instruction.
    instr = {k for k, g in groups.items() if INSTRUCTION_MEMBERS <= set(g)}
    if instr:
        before = len(rows)
        rows = [r for r in rows if tuple(_split(r)[0]) not in instr]
        excluded += before - len(rows)
    rows = [r for r in rows if tuple(_split(r)[0]) not in axes]
    # 1. Find Logix TIMER / COUNTER structures: a parent with ACC + PRE members.
    members: dict[tuple, dict] = {}
    for r in rows:
        area, leaf = _split(r)
        if leaf in TIMER_MEMBERS | COUNTER_MEMBERS and len(area) >= 1:
            members.setdefault(tuple(area), {})[leaf] = r
    structs = {k: v for k, v in members.items() if "ACC" in v and "PRE" in v}

    areas: "OrderedDict[str, dict]" = OrderedDict()

    def area_of(segs: Iterable[str]) -> dict:
        key = "/".join(segs) or "Tags"
        if key not in areas:
            areas[key] = {"id": key, "title": key.replace("/", " / "), "alarms": [], "timers": [],
                          "counters": [], "words": [], "values": [], "status": [], "hints": []}
        return areas[key]

    skipped = 0
    for key, mem in sorted(axes.items()):
        a = area_of(list(key[:-1]) or [AXES_AREA])
        any_nid = next(iter(mem.values()))["node_id"]
        a.setdefault("axes", []).append({
            "name": key[-1], "label": humanize(key[-1]), "base": any_nid.rsplit(".", 1)[0],
            "members": {k: mem[k]["node_id"] for k in AXIS_KEYS if k in mem}})
    for key, mem in sorted(structs.items()):
        parent, sname = list(key[:-1]), key[-1]
        if not parent:  # export rooted at the struct's folder: take the area from the NodeId
            parent = tag_path(next(iter(mem.values()))["node_id"])[-3:-2] or ["Tags"]
        is_counter = "CU" in mem or "CD" in mem or bool(_COUNT_NAME.search(sname))
        ctx = "/".join(tag_path(next(iter(mem.values()))["node_id"]))
        if not is_counter and not _FAULT_CONTEXT.search(ctx):
            skipped += 1  # logic timers outside fault folders aren't dashboard material
            continue
        item = {"name": sname, "label": humanize(sname),
                "members": {m: r["node_id"] for m, r in sorted(mem.items())}}
        a = area_of(parent)
        (a["counters"] if is_counter else a["timers"]).append(item)

    unlisted = 0
    running: list[tuple[int, str, str]] = []
    for r in rows:
        segs, leaf = _split(r)
        if tuple(segs) in structs:
            continue
        label_src = leaf
        if _SPARE.search(f"/{'/'.join(segs)}/{leaf}"):
            skipped += 1
            continue
        t = _type(r)
        full = "/".join(tag_path(r["node_id"])) + "/" + "/".join(segs)
        a = area_of(segs)
        item = {"node_id": r["node_id"], "name": leaf, "label": humanize(label_src)}
        if t in BOOL_TYPES:
            in_fault_context = bool(_FAULT_CONTEXT.search(full)) or bool(_FAULT_WORD.search(leaf))
            if in_fault_context and _COMMAND.search(leaf):
                unlisted += 1  # Reset_Faults and friends: a button, not a condition
            elif in_fault_context:
                cat = category(a["id"], leaf)
                sev = severity(leaf, cat)
                if sev == "status":
                    a["status"].append(item)
                else:
                    item.update(category=cat, severity=sev, says_fault=bool(_FAULT_WORD.search(leaf)),
                                sample=_truthy(r.get("value")))
                    if _OK_WHEN_ON.search(leaf):
                        item["ok_when_on"] = True
                    a["alarms"].append(item)
            else:
                score = _running_score(leaf)
                if score:
                    running.append((score, "/".join(segs), r["node_id"]))
                if _RELEVANT_STATUS.search(leaf) and not _NOT_STATUS.search(leaf):
                    a["status"].append(item)
                else:
                    unlisted += 1
        elif t in INT_TYPES:
            if _FAULT_WORD.search(leaf):
                a["words"].append(item)
            elif _COUNT_NAME.search(leaf):
                a["counters"].append(item)
            elif _RELEVANT_VALUE.search(leaf):
                a["values"].append(item)
            else:
                unlisted += 1
        elif t in REAL_TYPES:
            if _RELEVANT_VALUE.search(leaf) or len(rows) < 500:  # small exports: show every REAL
                a["values"].append(item)
            else:
                unlisted += 1
        else:
            skipped += 1

    capped = 0
    for a in areas.values():
        a["status"].sort(key=lambda x: (not _RELEVANT_STATUS.search(x["name"]), x["name"].lower()))
        for k in ("status", "values", "counters"):
            if len(a[k]) > MAX_PER_AREA:
                capped += len(a[k]) - MAX_PER_AREA
                del a[k][MAX_PER_AREA:]
        bits = [x for x in a["alarms"] if not x["says_fault"] and not x.get("ok_when_on")]
        on = sum(1 for x in bits if x["sample"])
        a["suggest_invert"] = len(bits) >= INVERT_MIN_BITS and on / len(bits) >= INVERT_SHARE
        if a["suggest_invert"]:
            a["hints"].append(f"{on} of {len(bits)} bits here were on when the tags were exported. "
                              "If on means healthy (e.g. comms OK bits), flip this area.")
        sev_rank = {"critical": 0, "fault": 1, "warning": 2}
        a["alarms"].sort(key=lambda x: (sev_rank.get(x["severity"], 3), x["name"].lower()))

    _titles(list(areas.values()))
    out_areas = [a for a in areas.values() if any(a.get(k) for k in ITEM_KINDS)]
    for a in out_areas:
        a.setdefault("axes", [])
        a["section"] = _section(a["id"])
    # Best guess at the machine's running bit: a plain name ("Running", "RunF") beats "Line_Run_Enable",
    # and controller scope beats a program's copy. Changeable on the dashboard.
    running.sort(key=lambda x: (-x[0], "program:" in x[1].lower(), len(x[1])))
    machine = {"running": [nid for _, _, nid in running[:1]], "mode": "any", "heartbeat": heartbeat,
               "freshness": "heartbeat" if heartbeat else "response", "max_read_ms": DEFAULT_MAX_READ_MS}
    summary = {
        "areas": len(out_areas),
        "alarms": sum(len(a["alarms"]) for a in out_areas),
        "timers": sum(len(a["timers"]) for a in out_areas),
        "counters": sum(len(a["counters"]) for a in out_areas),
        "axes": sum(len(a["axes"]) for a in out_areas),
        "skipped": skipped,
        "excluded": excluded,
        "capped": capped,
        "duplicates": duplicates,
        "unlisted": unlisted,
    }
    notes = []
    left_out = []
    if duplicates:
        left_out.append(f"{duplicates} program-parameter copies of controller tags")
    if unlisted:
        left_out.append(f"{unlisted} status bits and values with no recognisable job (searchable in Add tag)")
    if excluded:
        left_out.append(f"{excluded} I/O module tags, instruction tags (MSG, motion), strings and array elements "
                        f"(outside fault folders, or past [{MAX_ARRAY_INDEX}])")
    if capped:
        left_out.append(f"{capped} status bits and values past {MAX_PER_AREA} per area")
    if left_out:
        notes.append(f"Left out to keep the dashboard readable: {'; '.join(left_out)}. Add any of them back with Edit.")
    if not summary["counters"] and not summary["axes"]:
        notes.append("No counters found, so this dashboard shows health and faults only. "
                     "Export the run/production tags to add counts, run state and OEE.")
    return {"name": name, "areas": out_areas, "summary": summary, "notes": notes, "machine": machine}


def node_ids(layout: dict) -> list[str]:
    """Every NodeId the dashboard reads, in a stable order."""
    seen: "OrderedDict[str, None]" = OrderedDict()
    for a in layout.get("areas", []):
        for k in ITEM_KINDS:
            for item in a.get(k, []):
                if item.get("node_id"):
                    seen[item["node_id"]] = None
                for nid in item.get("members", {}).values():
                    seen[nid] = None
    m = layout.get("machine") or {}
    hb = m.get("heartbeat", []) if m.get("freshness", "heartbeat") == "heartbeat" else []
    for nid in [*m.get("running", []), *hb]:
        seen[nid] = None
    return list(seen)


ITEM_KINDS = ("alarms", "axes", "timers", "counters", "words", "values", "status")
SEVERITIES = ("critical", "fault", "warning")
MAX_LAYOUT_ITEMS = 200000  # dashboards built before whole-controller trimming can be this big


def clean_layout(layout: dict, known_ids: Optional[set] = None) -> dict:
    """Validate a layout edited in the UI; keep only known fields. Raises ValueError.

    ``known_ids``: NodeIds from the dashboard's tag export. When given, every tag on the dashboard must be one
    of them, so edits pick from what was discovered instead of free-typed NodeIds.
    """
    def text(v, n=200):
        if not isinstance(v, str) or len(v) > n:
            raise ValueError("bad text field")
        return v

    def nid(v):
        text(v, 1000)
        if known_ids is not None and v not in known_ids:
            raise ValueError(f"{v} is not in this dashboard's tag export")
        return v

    if not isinstance(layout, dict) or not isinstance(layout.get("areas"), list):
        raise ValueError("layout needs a list of areas")
    out, count = [], 0
    for a in layout["areas"]:
        if not isinstance(a, dict):
            raise ValueError("bad area")
        area = {"id": text(a.get("id")), "title": text(a.get("title", a.get("id"))),
                "section": text(a.get("section") or _section(a.get("id", ""))),
                "hints": [text(h, 500) for h in a.get("hints", [])][:5],
                "suggest_invert": bool(a.get("suggest_invert"))}
        for k in ITEM_KINDS:
            items = []
            for it in a.get(k, []):
                count += 1
                item = {"name": text(it.get("name", "")), "label": text(it.get("label", it.get("name", "")))}
                if it.get("node_id"):
                    item["node_id"] = nid(it["node_id"])
                if isinstance(it.get("members"), dict):
                    item["members"] = {text(m, 64): nid(v) for m, v in it["members"].items() if v}
                if k == "axes":
                    item["base"] = text(it.get("base", ""), 1000)
                if k == "alarms":
                    item["severity"] = it.get("severity") if it.get("severity") in SEVERITIES else "fault"
                    item["category"] = it.get("category") if it.get("category") in CATEGORY_TITLES else "other"
                    item["says_fault"] = bool(it.get("says_fault"))
                    item["sample"] = bool(it.get("sample"))
                    if it.get("ok_when_on"):
                        item["ok_when_on"] = True
                if not item.get("node_id") and not item.get("members"):
                    raise ValueError(f"{item['label'] or k} has no tag")
                items.append(item)
            area[k] = items
        out.append(area)
    if count > MAX_LAYOUT_ITEMS:
        raise ValueError("too many items")
    ids = [a["id"] for a in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate area id")
    m = layout.get("machine") or {}
    running = m.get("running") or []
    if not isinstance(running, list) or len(running) > 16:
        raise ValueError("bad running tags")
    heartbeat = m.get("heartbeat") or []
    if not isinstance(heartbeat, list) or len(heartbeat) > 1:
        raise ValueError("bad heartbeat tag")
    try:
        max_ms = min(max(int(m.get("max_read_ms") or DEFAULT_MAX_READ_MS), 100), 60000)
    except (TypeError, ValueError):
        raise ValueError("bad max_read_ms")
    machine = {"running": [nid(x) for x in running], "mode": "all" if m.get("mode") == "all" else "any",
               "heartbeat": [nid(x) for x in heartbeat],
               "freshness": m.get("freshness") if m.get("freshness") in FRESHNESS else ("heartbeat" if heartbeat else "response"),
               "max_read_ms": max_ms}
    result = dict(layout, areas=out, machine=machine)
    result["summary"] = dict(layout.get("summary", {}), areas=len(out),
                             alarms=sum(len(a["alarms"]) for a in out), timers=sum(len(a["timers"]) for a in out),
                             counters=sum(len(a["counters"]) for a in out), axes=sum(len(a["axes"]) for a in out))
    return result


# ---------------------------------------------------------------------------------------------- verification
HEARTBEAT_STALE_S = 15    # a heartbeat that hasn't changed for this long means the data may be frozen
_MISSING = ("BadNodeIdUnknown", "BadNodeIdInvalid", "BadAttributeIdInvalid")
_NO_ACCESS = ("BadNotReadable", "BadUserAccessDenied")


def plc_of(node_id: str) -> str:
    """The controller a FT Linx NodeId belongs to (``[LEVELER_01]``), for grouping comms problems."""
    m = re.search(r"\[([^\]]+)\]", node_id)
    return m.group(1) if m else "other"


def _status_finding(code: str, target: str, nid: str, status: str) -> dict:
    if status in _MISSING:
        return {"code": f"{code}.missing", "severity": "error", "target": target, "node_id": nid,
                "message": f"The gateway doesn't know this tag ({status}).",
                "hint": "The tag was renamed or deleted in the PLC, or the gateway's shortcut changed. "
                        "Edit the item and pick the current tag, or remove it."}
    if status in _NO_ACCESS:
        return {"code": f"{code}.denied", "severity": "error", "target": target, "node_id": nid,
                "message": f"The gateway refused to read this tag ({status}).",
                "hint": "Check the tag's External Access in the PLC and the OPC UA user's rights in FT Linx Gateway."}
    return {"code": f"{code}.unreadable", "severity": "error", "target": target, "node_id": nid,
            "message": f"The gateway returned {status} instead of a value.",
            "hint": "Usually comms between the gateway and the PLC: check the FT Linx shortcut and the PLC's path. "
                    "Other tags from the same PLC failing too points at comms, not the tag."}


def verify_layout(layout: dict, overrides: dict, reads: dict, live: Optional[dict] = None) -> dict:
    """Check that every alarm on a dashboard can be trusted to flag. Pure: no I/O.

    ``reads``: node_id -> {"status", "variant_type", "value"} from one read of the dashboard's tags.
    ``live``: what the live reads have seen so far ({"since": {node_id: last change}, "started": t, "now": t}).
    Returns {"summary": {...}, "findings": [...]}; findings carry a dotted ``code`` and a ``hint``.
    """
    live = live or {}
    since, started, now = live.get("since", {}), live.get("started"), live.get("now")
    invert = (overrides or {}).get("invert", {})
    hidden = set((overrides or {}).get("hidden", []))
    findings: list[dict] = []
    seen: dict[str, str] = {}
    checked = good = active = changed = 0
    bad_by_plc: dict[str, int] = {}

    def good_status(r):
        return r is not None and str(r.get("status", "")).startswith("Good")

    for a in layout.get("areas", []):
        alarms = [x for x in a.get("alarms", []) if x.get("node_id") not in hidden]
        on = 0
        readable_bits = 0
        for x in alarms:
            nid = x["node_id"]
            target = f"{a.get('title', a.get('id'))} / {x.get('label') or x.get('name')}"
            checked += 1
            if nid in seen:
                findings.append({"code": "alarm.duplicate", "severity": "warning", "target": target, "node_id": nid,
                                 "message": f"The same tag is also on the dashboard as {seen[nid]}.",
                                 "hint": "One of the two is probably meant to be a different bit. Edit or remove one."})
            seen.setdefault(nid, target)
            r = reads.get(nid)
            if r is None:
                findings.append({"code": "alarm.not_read", "severity": "error", "target": target, "node_id": nid,
                                 "message": "This tag wasn't read.", "hint": "Run the check again; if it persists, send the debug bundle."})
                continue
            if not good_status(r):
                bad_by_plc[plc_of(nid)] = bad_by_plc.get(plc_of(nid), 0) + 1
                findings.append(_status_finding("alarm", target, nid, r.get("status", "?")))
                continue
            good += 1
            vt = r.get("variant_type")
            if vt and vt != "Boolean":
                findings.append({"code": "alarm.not_bool", "severity": "warning", "target": target, "node_id": nid,
                                 "message": f"This alarm is a {vt}, not a BOOL, so it counts as active whenever it isn't 0.",
                                 "hint": "Pick the alarm bit itself, or move this tag to the area's values."})
            bit = _truthy(r.get("value"))
            if not x.get("ok_when_on"):
                readable_bits += 1
                on += bit
            if bit != (bool(invert.get(a.get("id"))) != bool(x.get("ok_when_on"))):
                active += 1
            if nid in since:
                changed += 1
        if readable_bits >= INVERT_MIN_BITS and on / readable_bits >= INVERT_SHARE and not invert.get(a.get("id")):
            findings.append({"code": "area.mostly_on", "severity": "warning", "target": a.get("title", a.get("id")),
                             "message": f"{on} of {readable_bits} alarm bits here are on right now.",
                             "hint": "If the machine is healthy, these bits mean OK (comms OK, guard closed). "
                                     "Tick Edit and set this area to \"On means healthy\"."})

    m = layout.get("machine") or {}
    for nid in m.get("running", []):
        r = reads.get(nid)
        if not good_status(r):
            findings.append(_status_finding("machine.running", "Machine running tag", nid, (r or {}).get("status", "not read")))
    response_only = m.get("freshness") == "response"
    hb = [] if response_only else m.get("heartbeat", [])
    if not hb and not response_only:
        findings.append({"code": "heartbeat.none", "severity": "info", "target": "Heartbeat",
                         "message": "No heartbeat tag, so frozen data can't be told apart from a quiet machine.",
                         "hint": "Tick Edit and pick a tag the PLC changes all the time, such as a free-running "
                                 "counter or a timer that always runs. A counter is better than a toggling bit."})
    for nid in hb:
        r = reads.get(nid)
        if not good_status(r):
            findings.append(_status_finding("heartbeat", "Heartbeat", nid, (r or {}).get("status", "not read")))
        elif started is not None and now is not None:
            age = now - since.get(nid, started)
            if age > HEARTBEAT_STALE_S:
                findings.append({"code": "heartbeat.frozen", "severity": "error", "target": "Heartbeat", "node_id": nid,
                                 "message": f"The heartbeat hasn't changed for {int(age)} s.",
                                 "hint": "The gateway may be serving old values: check FT Linx's connection to the PLC, "
                                         "or pick a heartbeat that really changes all the time."})
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    summary = {"alarms": checked, "readable": good, "active": active, "changed": changed, "bad_by_plc": bad_by_plc,
               "watching_s": int(now - started) if started is not None and now is not None else 0,
               "errors": sum(f["severity"] == "error" for f in findings),
               "warnings": sum(f["severity"] == "warning" for f in findings)}
    return {"summary": summary, "findings": findings}

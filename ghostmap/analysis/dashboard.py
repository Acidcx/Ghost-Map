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

# Keep live reads small: per area at most this many status bits, values and counters (alarms are all kept).
MAX_PER_AREA = 24
MAX_ARRAY_INDEX = 63      # array elements past this are skipped (recipe tables, data logs)
_MODULE_TAG = re.compile(r"^[^:/]+:\d+:[IOCS]$|^Local:", re.I)  # I/O module tags like Rack1:3:I
_ARRAY_IDX = re.compile(r"\[(\d+)(?:,\d+)*\]")
_RELEVANT_STATUS = re.compile(r"(run|auto|manual|mode|ready|enable|homed|state|cycle|start|stop|idle|hold)", re.I)

# An area where at least this share of the bits is on, and whose bit names
# don't say "fault", may be using "on = healthy" (e.g. comms OK bits).
INVERT_SHARE = 0.6
INVERT_MIN_BITS = 4

_FAULT_CONTEXT = re.compile(r"(fault|flt|alarm|alm|error|err\b|warn)", re.I)
_FAULT_WORD = re.compile(r"(fault|flt|alarm|alm|error|warn)", re.I)
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
    """Short area titles: the folder name when it's unique, else the path without program prefixes."""
    def trimmed(a):
        return [p for p in a["id"].split("/") if not p.lower().startswith("program:")] or [a["id"]]
    last = [trimmed(a)[-1] for a in areas]
    for a, short in zip(areas, last):
        a["title"] = short if last.count(short) == 1 else " / ".join(trimmed(a))


def _excluded(row: dict) -> bool:
    """I/O module tags and big array elements: thousands of tags that don't belong on a dashboard."""
    text = f"{row.get('path', '')}/{'/'.join(tag_path(row.get('node_id', '')))}"
    for seg in re.split(r"[/.]", text):
        if _MODULE_TAG.match(seg):
            return True
    return any(int(m.group(1)) > MAX_ARRAY_INDEX for m in _ARRAY_IDX.finditer(text))


def build_layout(rows: Iterable[dict], name: str = "Machine") -> dict:
    rows = [r for r in rows if r.get("node_id")]
    total = len(rows)
    rows = [r for r in rows if not _excluded(r)]
    excluded = total - len(rows)

    # 0. Motion axes: a parent whose members look like an AXIS_CIP_DRIVE.
    groups: dict[tuple, dict] = {}
    for r in rows:
        segs, leaf = _split(r)
        groups.setdefault(tuple(segs), {})[leaf] = r
    axes = {k: g for k, g in groups.items() if len(AXIS_SIGNATURE & set(g)) >= 4}
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
        if _ARRAY_IDX.search(leaf) and not _FAULT_CONTEXT.search(full):
            excluded += 1  # recipe tables, data logs: arrays only belong on a dashboard as fault bits
            continue
        a = area_of(segs)
        item = {"node_id": r["node_id"], "name": leaf, "label": humanize(label_src)}
        if t in BOOL_TYPES:
            in_fault_context = bool(_FAULT_CONTEXT.search(full)) or bool(_FAULT_WORD.search(leaf))
            if in_fault_context:
                cat = category(a["id"], leaf)
                sev = severity(leaf, cat)
                if sev == "status":
                    a["status"].append(item)
                else:
                    item.update(category=cat, severity=sev, says_fault=bool(_FAULT_WORD.search(leaf)),
                                sample=_truthy(r.get("value")))
                    a["alarms"].append(item)
            else:
                a["status"].append(item)
        elif t in INT_TYPES:
            if _FAULT_WORD.search(leaf):
                a["words"].append(item)
            elif _COUNT_NAME.search(leaf):
                a["counters"].append(item)
            else:
                a["values"].append(item)
        elif t in REAL_TYPES:
            a["values"].append(item)
        else:
            skipped += 1

    capped = 0
    for a in areas.values():
        a["status"].sort(key=lambda x: (not _RELEVANT_STATUS.search(x["name"]), x["name"].lower()))
        for k in ("status", "values", "counters"):
            if len(a[k]) > MAX_PER_AREA:
                capped += len(a[k]) - MAX_PER_AREA
                del a[k][MAX_PER_AREA:]
        bits = [x for x in a["alarms"] if not x["says_fault"]]
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
    summary = {
        "areas": len(out_areas),
        "alarms": sum(len(a["alarms"]) for a in out_areas),
        "timers": sum(len(a["timers"]) for a in out_areas),
        "counters": sum(len(a["counters"]) for a in out_areas),
        "axes": sum(len(a["axes"]) for a in out_areas),
        "skipped": skipped,
        "excluded": excluded,
        "capped": capped,
    }
    notes = []
    left_out = []
    if excluded:
        left_out.append(f"{excluded} I/O module tags and array elements (outside fault folders, or past [{MAX_ARRAY_INDEX}])")
    if capped:
        left_out.append(f"{capped} status bits and values past {MAX_PER_AREA} per area")
    if left_out:
        notes.append(f"Left out to keep live reads light: {' and '.join(left_out)}. Add any of them back with Edit.")
    if not summary["counters"] and not summary["axes"]:
        notes.append("No counters found, so this dashboard shows health and faults only. "
                     "Export the run/production tags to add counts, run state and OEE.")
    return {"name": name, "areas": out_areas, "summary": summary, "notes": notes}


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
    return list(seen)


ITEM_KINDS = ("alarms", "axes", "timers", "counters", "words", "values", "status")
SEVERITIES = ("critical", "fault", "warning")
MAX_LAYOUT_ITEMS = 20000


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
    result = dict(layout, areas=out)
    result["summary"] = dict(layout.get("summary", {}), areas=len(out),
                             alarms=sum(len(a["alarms"]) for a in out), timers=sum(len(a["timers"]) for a in out),
                             counters=sum(len(a["counters"]) for a in out), axes=sum(len(a["axes"]) for a in out))
    return result

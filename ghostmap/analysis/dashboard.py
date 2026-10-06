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
    """``EN_Motor_OverTemp_Warn`` -> ``EN Motor Over Temp Warning``. The raw name stays as a tooltip."""
    out = []
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


def build_layout(rows: Iterable[dict], name: str = "Machine") -> dict:
    rows = [r for r in rows if r.get("node_id")]
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
    for key, mem in sorted(structs.items()):
        parent, sname = list(key[:-1]), key[-1]
        if not parent:  # export rooted at the struct's folder: take the area from the NodeId
            parent = tag_path(next(iter(mem.values()))["node_id"])[-3:-2] or ["Tags"]
        is_counter = "CU" in mem or "CD" in mem or bool(_COUNT_NAME.search(sname))
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
        a = area_of(segs)
        t = _type(r)
        full = "/".join(tag_path(r["node_id"])) + "/" + "/".join(segs)
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

    for a in areas.values():
        bits = [x for x in a["alarms"] if not x["says_fault"]]
        on = sum(1 for x in bits if x["sample"])
        a["suggest_invert"] = len(bits) >= INVERT_MIN_BITS and on / len(bits) >= INVERT_SHARE
        if a["suggest_invert"]:
            a["hints"].append(f"{on} of {len(bits)} bits here were on when the tags were exported. "
                              "If on means healthy (e.g. comms OK bits), flip this area.")
        sev_rank = {"critical": 0, "fault": 1, "warning": 2}
        a["alarms"].sort(key=lambda x: (sev_rank.get(x["severity"], 3), x["name"].lower()))

    _titles(list(areas.values()))
    out_areas = [a for a in areas.values() if any(a[k] for k in ("alarms", "timers", "counters", "words", "values", "status"))]
    summary = {
        "areas": len(out_areas),
        "alarms": sum(len(a["alarms"]) for a in out_areas),
        "timers": sum(len(a["timers"]) for a in out_areas),
        "counters": sum(len(a["counters"]) for a in out_areas),
        "skipped": skipped,
    }
    notes = []
    if not summary["counters"]:
        notes.append("No counters found, so this dashboard shows health and faults only. "
                     "Export the run/production tags to add counts, run state and OEE.")
    return {"name": name, "areas": out_areas, "summary": summary, "notes": notes}


def node_ids(layout: dict) -> list[str]:
    """Every NodeId the dashboard reads, in a stable order."""
    seen: "OrderedDict[str, None]" = OrderedDict()
    for a in layout.get("areas", []):
        for k in ("alarms", "words", "values", "status", "counters"):
            for item in a[k]:
                if "node_id" in item:
                    seen[item["node_id"]] = None
                for nid in item.get("members", {}).values():
                    seen[nid] = None
        for t in a["timers"]:
            for nid in t["members"].values():
                seen[nid] = None
    return list(seen)

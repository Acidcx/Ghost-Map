"""Which alarms are active in one live read of a dashboard. Pure functions, no I/O.

The same rules as the Machine tab (``machine.js`` ``areaState``/``axisState``/``runningState``), so the
alarm history records exactly what the dashboard shows:

- an alarm bit is active when on; ``ok_when_on`` bits (``No_Faults``) are active when off, and an area
  flipped to "on = healthy" turns both around;
- a motion axis is faulted when its CIPAxisState is 8 (faulted) or a fault word is non-zero;
- a tag that came back with a Bad status is unknown: neither active nor cleared;
- hidden tags are left out.
"""

from __future__ import annotations

AXIS_FAULT_WORDS = ("AxisFault", "CIPAxisFaults", "ModuleFaults", "GuardFaults", "MotionFaultStatus",
                    "CIPInitializationFaults", "CIPAPRFaults", "AxisSafetyFaults")
AXIS_FAULTED_STATE = 8


def truthy(v) -> bool:
    return v is True or v == 1 or v == "true" or v == "1"


def _number(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def alarm_states(dash: dict, values: dict, bad) -> tuple[dict, set]:
    """``(active, known)`` for one read.

    ``active`` maps a key (the alarm's NodeId, or ``axis:<base>`` for a motion axis) to what it is:
    ``{"kind", "area", "label", "severity", "node_id"}``. ``known`` holds every key that could be read, active or
    not; a key missing from it was unreadable, so its state is not known.
    """
    bad = set(bad or ())
    ov = dash.get("overrides") or {}
    inverted = ov.get("invert") or {}
    hidden = set(ov.get("hidden") or [])

    def readable(nid):
        return bool(nid) and nid in values and nid not in bad

    active: dict[str, dict] = {}
    known: set[str] = set()
    for area in (dash.get("layout") or {}).get("areas", []):
        inv = bool(inverted.get(area["id"]))
        title = area.get("title") or area["id"]
        for a in area.get("alarms", []):
            nid = a.get("node_id")
            if not nid or nid in hidden or not readable(nid):
                continue
            known.add(nid)
            on = truthy(values[nid])
            if (not on) if inv != bool(a.get("ok_when_on")) else on:
                active[nid] = {"kind": "alarm", "area": area["id"], "area_title": title, "node_id": nid,
                               "label": a.get("label") or a.get("name") or nid,
                               "severity": a.get("severity") or "fault"}
        for x in area.get("axes", []):
            m = x.get("members") or {}
            state_id = m.get("CIPAxisState")
            if not readable(state_id):
                continue
            key = f"axis:{x.get('base') or x.get('name')}"
            known.add(key)
            words = [w for w in AXIS_FAULT_WORDS if readable(m.get(w)) and _number(values[m[w]])]
            if int(_number(values[state_id])) == AXIS_FAULTED_STATE or words:
                active[key] = {"kind": "axis", "area": area["id"], "area_title": title, "node_id": state_id,
                               "label": f"{x.get('label') or x.get('name')} axis fault"
                                        + (f" ({', '.join(words)})" if words else ""),
                               "severity": "fault"}
    return active, known


def running_state(layout: dict, values: dict, bad):
    """True / False when the machine's running tags can be read, None when they can't (or there are none)."""
    m = layout.get("machine") or {}
    ids = list(m.get("running") or [])
    bad = set(bad or ())
    ok = [n for n in ids if n in values and n not in bad]
    if not ids or not ok:
        return None
    on = [truthy(values[n]) for n in ok]
    return all(on) if m.get("mode") == "all" else any(on)

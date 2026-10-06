"""Dashboard generator. Rows are invented but shaped like real FT Linx Gateway exports."""

from ghostmap.analysis.dashboard import build_layout, category, humanize, node_ids, severity, tag_path

NS = "ns=2;s=::[LINE]Program:MainProgram.FAULT"


def row(path, typ="Boolean", value="false", base=NS):
    return {"path": path, "node_id": f"{base}.{path.replace('/', '.')}", "type": typ, "value": value}


def timer(path, acc="0", pre="1000"):
    return [row(f"{path}/{m}", "Boolean", "false") for m in ("EN", "TT", "DN")] + \
           [row(f"{path}/ACC", "Int32", acc), row(f"{path}/PRE", "Int32", pre)]


ROWS = [
    row("Feed/E_Stop"), row("Feed/Drive_Flt", value="true"), row("Feed/Motor_OverTemp_Warn"),
    row("Feed/Pump_MS"), row("Feed/Gear_Sts", value="true"), *timer("Feed/Pump_MS_Tmr"),
    row("Hyd/Low_Oil_Level"), row("Hyd/Filter_Plugged"),
    # Comms area where every bit is on: probably "on = healthy"
    *[row(f"Comms/Rack_{i:02d}", value="true") for i in range(6)],
    row("General/PLC_Status_Flts", "Int32", "0"), row("General/Spare/Spare[0]", "Int32", "0"),
]


def test_tag_path_strips_shortcut_and_program():
    assert tag_path("ns=2;s=::[PLC]Program:MainProgram.FAULT.Zone1.E_Stop") == ["FAULT", "Zone1", "E_Stop"]
    assert tag_path("ns=2;s=[PRESS_01]PressFireCount") == ["PressFireCount"]


def test_names():
    assert humanize("Motor_OverTemp_Warn") == "Motor Over Temp Warning"
    assert humanize("VFD_Comms_Flt") == "VFD Comms Fault"
    assert humanize("E_Stop") == "E-Stop"
    assert category("Zone1", "EStop_Circuit_Flt") == "estop"
    assert category("Comms", "VFD_01") == "comms"          # the folder wins over the name
    assert category("Entry", "Uncoiler_Drive_Flt") == "drive"  # "coil" is not hydraulic oil
    assert category("Zone1", "Pump_Low_Oil_Flt") == "hydraulic"
    assert severity("HPU_OverTemp_Warn", "temperature") == "warning"
    assert severity("E_Stop", "estop") == "critical"


def test_layout_groups_areas_timers_and_alarms():
    L = build_layout(ROWS, "Line")
    areas = {a["id"]: a for a in L["areas"]}
    assert set(areas) == {"Feed", "Hyd", "Comms", "General"}
    feed = areas["Feed"]
    assert [t["name"] for t in feed["timers"]] == ["Pump_MS_Tmr"]
    assert set(feed["timers"][0]["members"]) == {"EN", "TT", "DN", "ACC", "PRE"}
    alarms = {a["name"]: a for a in feed["alarms"]}
    assert alarms["E_Stop"]["severity"] == "critical"
    assert alarms["Motor_OverTemp_Warn"]["severity"] == "warning"
    assert alarms["Pump_MS"]["category"] == "motor"
    assert feed["alarms"][0]["name"] == "E_Stop"  # critical first
    assert [s["name"] for s in feed["status"]] == ["Gear_Sts"]
    assert [w["name"] for w in areas["General"]["words"]] == ["PLC_Status_Flts"]
    assert L["summary"]["skipped"] == 1  # the spare
    assert L["notes"] and "No counters" in L["notes"][0]


def test_suggests_flipping_areas_that_are_mostly_on():
    areas = {a["id"]: a for a in build_layout(ROWS)["areas"]}
    assert areas["Comms"]["suggest_invert"] and areas["Comms"]["hints"]
    assert not areas["Feed"]["suggest_invert"]


def test_export_rooted_at_an_area_takes_the_area_from_the_node_id():
    # Exporting just one fault folder gives single-segment paths; the area comes from the NodeId.
    rows = [dict(r, path=r["path"].split("/", 1)[1]) for r in ROWS if r["path"].startswith("Feed/")]
    L = build_layout(rows)
    assert [a["id"] for a in L["areas"]] == ["Feed"]
    assert L["areas"][0]["timers"] and len(L["areas"][0]["alarms"]) == 4


def test_counters_and_values_outside_fault_folders():
    base = "ns=2;s=::[LINE]Program:MainProgram.Production"
    rows = [row("Line_Running", value="true", base=base), row("Coil_Count", "Int32", "12", base=base),
            row("Line_Speed", "Float", "120.5", base=base)]
    L = build_layout(rows)
    a = L["areas"][0]
    assert [x["name"] for x in a["status"]] == ["Line_Running"]
    assert [x["name"] for x in a["counters"]] == ["Coil_Count"]
    assert [x["name"] for x in a["values"]] == ["Line_Speed"]
    assert not a["alarms"] and not L["notes"]


def test_node_ids_cover_every_item_once():
    ids = node_ids(build_layout(ROWS))
    assert len(ids) == len(set(ids)) == len(ROWS) - 1  # all but the spare

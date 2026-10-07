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


def axis_rows(name="Ax_Feed"):
    base = f"ns=2;s=[LINE]{name}"
    members = {"ActualPosition": "Float", "ActualVelocity": "Float", "CIPAxisState": "Int16", "AxisFault": "Int32",
               "DriveEnableStatus": "Boolean", "CIPAxisFaults": "Int64", "VelocityLoopBandwidth": "Float",
               "BusUndervoltageFault": "Boolean", "BusUndervoltageAlarm": "Boolean", "AxisHomedStatus": "Boolean"}
    return [{"path": m, "node_id": f"{base}.{m}", "type": t, "value": "0"} for m, t in members.items()]


def test_motion_axis_collapses_to_one_item():
    # Real-world: exporting one AXIS_CIP_DRIVE gave ~580 members (~190 *Fault bits); it becomes one axis.
    L = build_layout(axis_rows())
    assert [a["title"] for a in L["areas"]] == ["Motion axes"]
    ax = L["areas"][0]["axes"][0]
    assert ax["name"] == "Ax_Feed" and ax["base"] == "ns=2;s=[LINE]Ax_Feed"
    assert {"CIPAxisState", "ActualPosition", "AxisFault", "CIPAxisFaults"} <= set(ax["members"])
    assert "BusUndervoltageFault" not in ax["members"]  # fault bits are read on request, not every second
    assert not L["areas"][0]["alarms"] and len(node_ids(L)) == 7


def test_module_tags_big_arrays_and_long_lists_are_left_out():
    rows = [row("Local:1:I/Data", "Int32", "0", base="ns=2;s=[LINE]"),
            *[row(f"Feed/Len[{i}]", "Float", "0", base="ns=2;s=[LINE]Program:P.Recipe") for i in range(100)],
            *[row(f"Flt_Bits/Flt[{i}]", value="false") for i in range(70)],
            *[row(f"Feed/Auto_{i:02d}", value="false", base="ns=2;s=[LINE]Program:P.IO") for i in range(40)],
            *[row(f"Feed/Bit_{i:02d}", value="false", base="ns=2;s=[LINE]Program:P.IO") for i in range(5)]]
    L = build_layout(rows)
    s = L["summary"]
    assert s["excluded"] == 1 + 100 + 6  # the module tag, the recipe array (no fault folder), Flt[64..69]
    assert s["capped"] == 40 - 12        # status bits past 12 per area
    assert s["unlisted"] == 5            # bits with no recognisable job stay searchable, off the screen
    assert L["notes"] and "Add any of them back" in L["notes"][0]
    flt = next(a for a in L["areas"] if a["id"] == "Flt_Bits")
    assert len(flt["alarms"]) == 64  # fault bit arrays stay, up to [63]


def test_logic_timers_outside_fault_folders_are_skipped():
    rows = timer("Seq/Step_Tmr", acc="5")
    for r in rows:
        r["node_id"] = r["node_id"].replace("FAULT.", "Logic.")
    assert not build_layout(rows)["areas"]


def test_clean_layout_checks_tags_against_the_export():
    import pytest

    from ghostmap.analysis.dashboard import clean_layout

    L = build_layout(ROWS)
    known = {r["node_id"] for r in ROWS}
    assert clean_layout(L, known)["summary"]["alarms"] == L["summary"]["alarms"]
    L["areas"][0]["alarms"][0]["node_id"] = "ns=2;s=Typed.By.Hand"
    with pytest.raises(ValueError):
        clean_layout(L, known)
    with pytest.raises(ValueError):
        clean_layout({"areas": [{"id": "a", "alarms": [{"name": "x"}]}]})


def test_camel_case_names():
    assert humanize("BusUndervoltageFault") == "Bus Undervoltage Fault"
    assert humanize("CIPAxisState") == "CIP Axis State"


def test_program_parameter_copies_of_controller_tags_are_dropped():
    # Real-world: FT Linx shows a program's InOut parameters as tags of their own, so a full-controller export
    # had one fault UDT ~13 times and one 12,000-tag structure 9 times. Copies of controller tags go; look-alike
    # program-local tags stay.
    def flags(prefix, node_prefix, on="false"):
        return [{"path": f"{prefix}/{n}", "node_id": f"{node_prefix}.{n}", "type": "Boolean",
                 "value": on if n == "Drive_Flt" else "false"} for n in
                ("Drive_Flt", "E_Stop", "Guard_Open", "Pump_MS", "Low_Oil_Flt", "OverTemp", "Comms_Flt", "Air_Low")]
    rows = (flags("Online/Faults/Zone1", "ns=2;s=[PLC]Faults.Zone1", on="true")
            + flags("Online/Program:Main/FAULT/Zone1", "ns=2;s=::[PLC]Program:Main.FAULT.Zone1", on="true")
            + flags("Online/Program:Other/Flts/Zone1", "ns=2;s=::[PLC]Program:Other.Flts.Zone1", on="true")
            + flags("Online/Program:A/Local_Faults", "ns=2;s=::[PLC]Program:A.Local_Faults")
            + flags("Online/Program:B/Local_Faults", "ns=2;s=::[PLC]Program:B.Local_Faults"))
    L = build_layout(rows)
    assert L["summary"]["duplicates"] == 16
    by_section = sorted((a["section"], a["title"]) for a in L["areas"])
    assert by_section == [("A", "Local_Faults"), ("B", "Local_Faults"), ("Controller", "Zone1")]


def test_instruction_tags_and_strings_are_left_out():
    base = "ns=2;s=[PLC]"
    rows = [{"path": f"Read_Msg/{m}", "node_id": f"{base}Read_Msg.{m}", "type": "Boolean", "value": "false"}
            for m in ("EN", "DN", "ER", "EW", "ST")]
    rows += [{"path": "Name/LEN", "node_id": f"{base}Name.LEN", "type": "Int32", "value": "3"},
             {"path": "Name/DATA/DATA[00]", "node_id": f"{base}Name.DATA[0]", "type": "SByte", "value": "65"}]
    rows += [row("Feed/Drive_Flt")]
    L = build_layout(rows)
    assert [a["title"] for a in L["areas"]] == ["Feed"]


def test_running_guess_prefers_a_plain_running_bit():
    base = "ns=2;s=[PLC]Line."
    names = ["Auto_Batch_Runout", "Run_Time_Hrs", "Line_Run_Enable", "Running", "Run"]
    rows = [{"path": f"Line/{n}", "node_id": base + n, "type": "Boolean", "value": "false"} for n in names]
    assert build_layout(rows)["machine"]["running"] == [base + "Running"]


def test_clean_layout_checks_running_tags():
    import pytest

    from ghostmap.analysis.dashboard import clean_layout

    L = build_layout(ROWS)
    known = {r["node_id"] for r in ROWS}
    L["machine"] = {"running": [ROWS[0]["node_id"]], "mode": "all"}
    assert clean_layout(L, known)["machine"] == {"running": [ROWS[0]["node_id"]], "mode": "all", "heartbeat": []}
    L["machine"]["running"] = ["ns=2;s=Made.Up"]
    with pytest.raises(ValueError):
        clean_layout(L, known)


def test_verify_layout_flags_alarms_that_cant_be_trusted():
    from ghostmap.analysis.dashboard import node_ids, verify_layout

    def alarm(name):
        return {"name": name, "label": name, "node_id": f"ns=2;s=[PLC1]FAULT.Comms.{name}", "severity": "fault",
                "category": "comms"}

    bits = [alarm(n) for n in ("Rack1_OK", "Rack2_OK", "Rack3_OK", "Rack4_OK")]
    gone, word = alarm("Old_Name_Flt"), alarm("Fault_Code")
    L = {"areas": [{"id": "Comms", "title": "Comms", "alarms": bits},
                   {"id": "General", "title": "General", "alarms": [gone, word, dict(word, label="again")]}],
         "machine": {"running": ["ns=2;s=[PLC1]RunF"], "heartbeat": []}}
    assert "ns=2;s=[PLC1]RunF" in node_ids(L)  # machine tags are read even when no area shows them
    reads = {b["node_id"]: {"status": "Good", "variant_type": "Boolean", "value": True} for b in bits}
    reads[gone["node_id"]] = {"status": "BadNodeIdUnknown", "variant_type": None, "value": None}
    reads[word["node_id"]] = {"status": "Good", "variant_type": "Int32", "value": 0}
    reads["ns=2;s=[PLC1]RunF"] = {"status": "BadCommunicationError", "variant_type": None, "value": None}
    out = verify_layout(L, {}, reads)
    codes = [f["code"] for f in out["findings"]]
    assert {"alarm.missing", "machine.running.unreadable", "alarm.not_bool", "alarm.duplicate", "area.mostly_on",
            "heartbeat.none"} <= set(codes)
    assert all(f["hint"] for f in out["findings"])
    assert out["summary"]["bad_by_plc"] == {"PLC1": 1} and out["summary"]["active"] == 4
    # Flipped to "on means healthy": the OK bits are no longer active, and no longer flagged.
    flipped = verify_layout(L, {"invert": {"Comms": True}}, reads)
    assert "area.mostly_on" not in {f["code"] for f in flipped["findings"]} and flipped["summary"]["active"] == 0


def test_heartbeat_guess_prefers_counters():
    rows = [{"path": "Online/Prog/HMI_Heartbeat", "node_id": "ns=2;s=::[P]Program:Prog.HMI_Heartbeat", "type": "Boolean", "value": True},
            {"path": "Online/Heartbeat_Cnt", "node_id": "ns=2;s=::[P]Heartbeat_Cnt", "type": "Int32", "value": 5},
            {"path": "Online/FAULT/E_Stop_Flt", "node_id": "ns=2;s=::[P]FAULT.E_Stop_Flt", "type": "Boolean", "value": False}]
    assert build_layout(rows)["machine"]["heartbeat"] == ["ns=2;s=::[P]Heartbeat_Cnt"]
    assert build_layout(rows[2:])["machine"]["heartbeat"] == []

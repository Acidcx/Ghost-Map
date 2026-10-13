"""Alarm history: events, stops with first-out, comms gaps, restarts and retention."""

from ghostmap.analysis.alarms import alarm_states, running_state
from ghostmap.history import History

ESTOP, DRIVE, AIR, OK = "ns=2;s=[P]F.E_Stop", "ns=2;s=[P]F.Drive_Flt", "ns=2;s=[P]F.Low_Air", "ns=2;s=[P]C.Rack_OK"
RUN = "ns=2;s=[P]Running"
AX = {"CIPAxisState": "ns=2;s=[P]Ax_Roll.CIPAxisState", "AxisFault": "ns=2;s=[P]Ax_Roll.AxisFault"}


def dash(invert_comms=True, hidden=()):  # the comms-OK area flipped, as on a real dashboard
    areas = [
        {"id": "Faults", "title": "Faults", "alarms": [
            {"name": "E_Stop", "label": "E Stop", "node_id": ESTOP, "severity": "critical"},
            {"name": "Drive_Flt", "label": "Drive Flt", "node_id": DRIVE},
            {"name": "Low_Air", "label": "Low Air", "node_id": AIR, "severity": "warning"}],
         "axes": [{"name": "Ax_Roll", "label": "Ax Roll", "base": "ns=2;s=[P]Ax_Roll", "members": AX}]},
        {"id": "Comms", "title": "Comms", "alarms": [{"name": "Rack_OK", "label": "Rack OK", "node_id": OK}]},
    ]
    return {"id": "lev", "layout": {"areas": areas, "machine": {"running": [RUN], "mode": "any"}},
            "overrides": {"invert": {"Comms": True} if invert_comms else {}, "hidden": list(hidden)}}


def read(on=(), bad=(), running=True, axis_state=4, ok=True):
    if not ok:
        return {"ok": False, "error": "TimeoutError: no answer from the gateway in time", "values": {}, "bad": []}
    values = {n: n in on for n in (ESTOP, DRIVE, AIR)}
    values.update({OK: True, RUN: running, AX["CIPAxisState"]: axis_state, AX["AxisFault"]: 0})
    for n in bad:
        values[n] = None
    return {"ok": True, "values": values, "bad": list(bad)}


def test_alarm_states_follow_the_dashboard_rules():
    d = dash(invert_comms=True)
    active, known = alarm_states(d, read(on=[ESTOP], axis_state=8)["values"], [])
    assert set(active) == {ESTOP, "axis:ns=2;s=[P]Ax_Roll"} and active[ESTOP]["severity"] == "critical"
    assert OK in known and OK not in active  # a comms-OK bit that is on is healthy in a flipped area
    v = read()["values"]
    v[OK] = False
    assert OK in alarm_states(d, v, [])[0]
    active, known = alarm_states(dash(hidden=[ESTOP]), read(on=[ESTOP], bad=[DRIVE])["values"], [DRIVE])
    assert ESTOP not in active and DRIVE not in known  # hidden: ignored; Bad status: unknown, not cleared
    assert running_state(d["layout"], read(running=False)["values"], []) is False
    assert running_state(d["layout"], read()["values"], [RUN]) is None


def test_stop_with_first_out_then_cascade(tmp_path):
    h = History(tmp_path)
    d = dash()
    h.observe(d, read(), now=100)                          # machine clear
    cur = h.observe(d, read(on=[ESTOP]), now=101)          # E-stop first ...
    assert cur["first_out"] == [ESTOP]
    h.observe(d, read(on=[ESTOP, DRIVE], running=False), now=103)  # ... the drive faults as a result
    h.observe(d, read(on=[DRIVE], running=False), now=110)
    h.observe(d, read(running=False), now=115)             # all clear: the stop ends
    assert h.current("lev") == {"stop": None, "first_out": []}

    stops = h.stops("lev", 0, 200)
    assert len(stops) == 1
    s = stops[0]
    assert (s["start"], s["end"], s["alarms"], s["tie"], s["first_known"], s["was_running"]) == (101, 115, 2, 1, 1, 1)
    assert [f["key"] for f in s["first_out"]] == [ESTOP]
    ev = {e["key"]: e for e in h.events("lev", 0, 200)}
    assert (ev[ESTOP]["start"], ev[ESTOP]["end"], ev[ESTOP]["first_out"]) == (101, 110, 1)
    assert (ev[DRIVE]["start"], ev[DRIVE]["end"], ev[DRIVE]["first_out"]) == (103, 115, 0)
    sm = h.summary("lev", 0, 200)
    assert sm["stops"] == 1 and sm["alarms"] == 2 and sm["first_out"][0]["key"] == ESTOP
    assert sm["longest"][0]["key"] == DRIVE and sm["longest"][0]["seconds"] == 12
    csv = h.events_csv("lev", 0, 200)
    assert csv.splitlines()[0].startswith("start,end,seconds,first_out") and "E Stop" in csv
    runs = h._rows("SELECT at, running FROM runs WHERE dash = 'lev' ORDER BY at", ())
    assert [(r["at"], r["running"]) for r in runs] == [(100, 1), (103, 0)]


def test_ties_alarms_on_at_start_and_axis_faults(tmp_path):
    h = History(tmp_path)
    d = dash()
    h.observe(d, read(on=[AIR]), now=10)                   # already on when recording started
    s = h.stops("lev", 0, 100)[0]
    assert s["first_known"] == 0 and s["first_out"] == []
    assert h.events("lev", 0, 100)[0]["at_start"] == 1
    h.observe(d, read(), now=11)
    cur = h.observe(d, read(on=[ESTOP, DRIVE], axis_state=8), now=20)  # three in the same read: a tie
    assert set(cur["first_out"]) == {ESTOP, DRIVE, "axis:ns=2;s=[P]Ax_Roll"}
    assert h.stops("lev", 0, 100)[0]["tie"] == 3
    assert any("axis fault" in e["label"] for e in h.events("lev", 0, 100))


def test_comms_gap_freezes_states_and_unreadable_tags_stay_open(tmp_path):
    h = History(tmp_path)
    d = dash()
    h.observe(d, read(), now=0)
    h.observe(d, read(on=[ESTOP]), now=1)
    h.observe(d, read(ok=False), now=2)                    # gateway gone
    h.observe(d, read(ok=False), now=5)
    h.observe(d, read(), now=9)                            # back: E-stop cleared at some point in the gap
    e = h.events("lev", 0, 100)[0]
    assert e["end"] == 9 and e["end_uncertain"] == 1
    gaps = h._rows("SELECT start, end FROM gaps", ())
    assert [(g["start"], g["end"]) for g in gaps] == [(2, 9)]
    assert h.summary("lev", 0, 100)["comms_gap_seconds"] == 7
    # A tag reading Bad is unknown: its event stays open instead of being cleared.
    h.observe(d, read(on=[DRIVE]), now=20)
    h.observe(d, read(bad=[DRIVE]), now=21)
    assert h.events("lev", 0, 100)[0]["end"] is None
    h.observe(d, read(), now=22)
    assert h.events("lev", 0, 100)[0]["end"] == 22


def test_restart_closes_open_rows_and_old_rows_age_out(tmp_path):
    h = History(tmp_path)
    d = dash()
    h.observe(d, read(), now=1000)
    h.observe(d, read(on=[ESTOP]), now=1005)               # still on when Ghost Map stops
    h.close()
    h = History(tmp_path)                                   # restart: closed at the last "still recording" time
    e = h.events("lev", 0, 2000)[0]
    assert e["end"] == 1005 and e["end_uncertain"] == 1
    assert h.stops("lev", 0, 2000)[0]["end_uncertain"] == 1
    h._prune(1005 + h.retention_s + 1)
    assert h.events("lev", 0, 10 ** 10) == [] and h.stops("lev", 0, 10 ** 10) == []


def test_hour_meters_count_running_and_online_time(tmp_path):
    h = History(tmp_path)
    d = dash()
    t0 = 1_791_000_000.0
    h.observe(d, read(running=False), now=t0)
    for i in range(1, 7):                                   # 6 s stopped, then 6 s running, one read a second
        h.observe(d, read(running=False), now=t0 + i)
    for i in range(7, 13):
        h.observe(d, read(running=True), now=t0 + i)
    h.observe(d, read(ok=False), now=t0 + 14)              # a comms gap is neither online nor running
    h.observe(d, read(running=True), now=t0 + 40)
    h.observe(d, read(running=True), now=t0 + 41)
    m = h.meters("lev")
    assert m["online_s"]["value"] == 13 and m["run_s"]["value"] == 6  # t0+7..12 and t0+40..41 while running
    h.close()
    h = History(tmp_path)                                   # lifetime totals survive a restart
    assert h.meters("lev")["run_s"]["value"] == 6 and h.counts()["meters"] == 2
    assert h.meter_window("lev", "run_s", 36500) == 6

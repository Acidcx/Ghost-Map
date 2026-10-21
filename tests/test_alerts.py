"""The alert list (analysis/alerts.py rules, web/alerts.py AlertCenter) and the HMI's own health
(collectors/host.py)."""

import asyncio
import io
import json
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

from ghostmap import winevent
from ghostmap.analysis import alerts as rules
from ghostmap.collectors.host import HostMonitor, parse_powershell, summarize_events
from ghostmap.web.alerts import AlertCenter
from ghostmap.web.app import create_app

H = {"X-Ghostmap": "1"}
GB = 1024 ** 3
NOW = 1_760_000_000.0


def codes(alerts):
    return sorted((a["code"], a["severity"]) for a in alerts)


def test_host_alerts():
    snap = {"disks": [{"path": "C:", "total": 128 * GB, "free": 10 * GB, "used_pct": 92.2, "data": True},
                      {"path": "D:", "total": 500 * GB, "free": 300 * GB, "used_pct": 40.0}],
            "memory": {"total": 16 * GB, "available": GB // 2, "used_pct": 96.9},
            "cpu": {"avg_5m": 95.0},
            "services": [{"name": "RSLinx Enterprise", "display": "FactoryTalk Linx", "state": "Stopped", "start_mode": "Auto"},
                         {"name": "FTDiag", "display": "FactoryTalk Diagnostics", "state": "Stopped", "start_mode": "Auto"},
                         {"name": "FTHist", "display": "FactoryTalk View SE Historian", "state": "Stopped", "start_mode": "Manual"},
                         {"name": "RsDirMux", "display": "Rockwell Directory Multiplexor", "state": "Running", "start_mode": "Auto"}],
            "events": {"window_h": 24, "top": [
                {"log": "System", "source": "Kernel-Power", "id": 41, "level": "critical", "count": 1, "last": NOW, "message": "x"},
                {"log": "Application", "source": "FT Diag", "id": 3, "level": "error", "count": 14, "last": NOW, "message": "y"},
                {"log": "Application", "source": "Other", "id": 9, "level": "error", "count": 2, "last": NOW, "message": "z"}]}}
    got = rules.host_alerts(snap)
    assert codes(got) == [("hmi.cpu_high", "warning"), ("hmi.disk_low", "warning"), ("hmi.event_critical", "warning"),
                          ("hmi.event_errors", "warning"), ("hmi.memory_high", "error"),
                          ("hmi.service_stopped", "error"), ("hmi.service_stopped", "warning")]
    by = {a["key"]: a for a in got}
    assert "Ghost Map's data is on it" in by["hmi.disk_low:C:"]["detail"]
    linx = by["hmi.service_stopped:RSLinx Enterprise"]
    assert linx["severity"] == "error" and "can't reach the PLCs" in linx["detail"]
    assert all(a["hint"] and a["link"] == {"tab": "hostpc"} for a in got)

    # Less than half a GB free is an error whatever the percentage; a healthy PC has nothing to say.
    assert rules.host_alerts({"disks": [{"path": "/", "total": 4 * GB, "free": GB // 4, "used_pct": 93.8}]})[0]["severity"] == "error"
    assert rules.host_alerts({"disks": [{"path": "C:", "total": 128 * GB, "free": 64 * GB, "used_pct": 50.0}],
                              "memory": {"total": 16 * GB, "available": 8 * GB, "used_pct": 50.0}, "cpu": {"avg_5m": None},
                              "services": None, "events": None}) == []


def test_comms_and_stop_alerts():
    dash = {"id": "lev", "name": "Leveler"}
    down = rules.comms_alerts(dash, {"last_ok": NOW - 300, "last_error": "TimeoutError"}, {"error": "TimeoutError"}, NOW)
    assert codes(down) == [("comms.down", "error")] and down[0]["link"] == {"tab": "machine", "dash": "lev", "view": "health"}
    # A single failed read right after a good one isn't "down" yet.
    assert rules.comms_alerts(dash, {"last_ok": NOW - 5}, {"error": "TimeoutError", "last_ok": NOW - 5}, NOW) == []

    health = {"last_ok": NOW, "heartbeat": {"frozen": True, "age_s": 42}, "total": 120, "good": 117,
              "bad_by_plc": {"PLC1": 3}, "cert_changed": {"sha256": "ab" * 32}}
    got = rules.comms_alerts(dash, health, {"last_ok": NOW}, NOW)
    assert codes(got) == [("comms.certificate_changed", "warning"), ("comms.frozen", "error"), ("comms.tags_bad", "warning")]
    assert "3 of 120" in [a for a in got if a["code"] == "comms.tags_bad"][0]["title"]

    stop = {"start": NOW - 1500, "end": None, "first_out": [{"label": "E-stop pressed", "area_title": "Shear"}]}
    got = rules.stop_alerts(dash, stop, NOW)
    assert got[0]["title"] == "Leveler stopped for 25 min" and "E-stop pressed (Shear)" in got[0]["detail"]
    assert got[0]["at"] == stop["start"]
    assert rules.stop_alerts(dash, {**stop, "start": NOW - 60}, NOW) == []
    assert rules.stop_alerts(dash, {**stop, "end": NOW - 10}, NOW) == []
    assert rules.stop_alerts(dash, None, NOW) == []


def test_network_maintenance_and_tsc_alerts():
    open_events = [
        {"code": "port.broadcast_storm", "severity": "error", "switch": "10.0.0.2", "name": "SW1", "port": "Fa1/3",
         "message": "storm", "peak": 4200, "unit": "pkt/s", "hint": "find the loop", "start": NOW - 60},
        {"code": "port.broadcast_storm", "severity": "error", "switch": "10.0.0.1", "name": "SW0", "port": "Gi1/1",
         "message": "storm", "start": NOW - 60, "passing_from": ["SW1 Fa1/3"]},  # the same storm on the uplink
        {"code": "port.info", "severity": "info", "switch": "10.0.0.2", "port": "Fa1/4", "message": "x", "start": NOW}]
    status = {"interval_s": 30, "switches": [
        {"switch": "10.0.0.3", "name": "SW3", "error": "timeout", "last_ok": NOW - 200, "error_at": NOW - 10},
        {"switch": "10.0.0.4", "name": "SW4", "error": "timeout", "last_ok": NOW - 40, "error_at": NOW - 10}]}
    got = rules.switch_alerts(open_events, status, NOW)
    assert [a["key"] for a in got] == ["port.broadcast_storm:10.0.0.2:Fa1/3", "switch.unreachable:10.0.0.3"]
    assert got[0]["detail"] == "Peak 4,200 pkt/s"

    events = [{"code": "passive.ip_conflict", "at": NOW - 100, "mac": "aa", "text": "old"},
              {"code": "passive.ip_conflict", "at": NOW - 50, "mac": "aa", "text": "new"},
              {"code": "passive.bootp", "at": NOW - 2 * 86400, "mac": "bb", "text": "two days ago"},
              {"code": "passive.syslog", "at": NOW, "text": "not an alert"}]
    got = rules.network_alerts(events, NOW)
    assert [(a["code"], a["severity"], a["title"]) for a in got] == [("network.ip_conflict", "error", "new")]

    items = [{"id": "a", "name": "Punch", "state": "overdue", "value": 1200, "interval": 1000, "unit": "hits"},
             {"id": "b", "name": "Oil", "state": "soon", "value": 950, "interval": 1000}]
    got = rules.maintenance_alerts(items)
    assert len(got) == 1 and got[0]["detail"] == "200 hits past its 1,000 hits interval"

    assert rules.tsc_alerts({"configured": False, "last_error": "x"}, NOW) == []
    assert rules.tsc_alerts({"configured": True, "last_error": "x", "last_error_at": NOW - 5, "last_ok": NOW}, NOW) == []
    assert codes(rules.tsc_alerts({"configured": True, "last_error": "login failed", "last_error_at": NOW, "last_ok": NOW - 60},
                                  NOW)) == [("production.tsc_unreachable", "warning")]


def test_alert_center_reports_raised_and_cleared():
    logged = []
    found = {"a": [], "b": []}

    def src(name):
        def get(now):
            if found[name] is None:
                raise RuntimeError("no answer")
            return found[name]
        return get

    async def b_source(now):  # sources may be async
        return src("b")(now)

    center = AlertCenter({"a": src("a"), "b": b_source}, eventlog=lambda *a: logged.append(a))
    run = lambda now: asyncio.run(center.evaluate(now))  # noqa: E731
    disk = rules.alert("hmi.disk_low:C:", "hmi.disk_low", "warning", "hmi", "Disk C: is 92% full", "", "free space")
    stop = rules.alert("machine.stopped:lev", "machine.stopped", "warning", "machine", "Leveler stopped", "", "look", at=NOW - 900)

    found["a"] = [disk]
    found["b"] = [stop]
    got = run(NOW)
    assert [a["key"] for a in got] == ["hmi.disk_low:C:", "machine.stopped:lev"]  # newest first among equals
    assert got[1]["since"] == NOW - 900 and got[0]["since"] == NOW  # an alert's own start time wins
    assert [(k, i) for k, i, _ in logged] == [(winevent.WARNING, 100), (winevent.WARNING, 300)]
    assert "What to do: free space" in logged[0][2]

    logged.clear()
    run(NOW + 30)  # nothing new: nothing reported, and the start time is kept
    assert logged == [] and center.active["hmi.disk_low:C:"]["since"] == NOW

    found["a"] = [{**disk, "severity": "error", "title": "Disk C: is 98% full"}]  # worse: reported again
    found["b"] = None  # a broken source keeps its alerts rather than clearing them
    run(NOW + 60)
    assert [(k, i) for k, i, _ in logged] == [(winevent.ERROR, 100)]
    assert center.active["hmi.disk_low:C:"]["since"] == NOW and "machine.stopped:lev" in center.active
    assert "RuntimeError" in center.view()["errors"]["b"]

    logged.clear()
    found["a"], found["b"] = [], []
    run(NOW + 90)
    assert sorted((k, i) for k, i, _ in logged) == [(winevent.INFO, 101), (winevent.INFO, 301)]
    assert all(t.startswith("Cleared: ") for *_, t in logged)
    view = center.view()
    assert view["alerts"] == [] and view["counts"] == {"error": 0, "warning": 0} and view["errors"] == {}
    assert [r["event"] for r in view["recent"]][:2] == ["cleared", "cleared"] and len(view["recent"]) == 5

    # The event log failing never breaks the list.
    center.eventlog = lambda *a: 1 / 0
    found["a"] = [disk]
    assert len(run(NOW + 120)) == 1


def test_powershell_output():
    """Services and event-log errors as PowerShell's ConvertTo-Json writes them: a single result is an object,
    not a list, and none at all is null."""
    out = json.dumps({"services": {"Name": "FTLinxGW", "DisplayName": "FactoryTalk Linx Gateway", "State": "Running",
                                   "StartMode": "Auto"},
                      "events": [{"LogName": "System", "ProviderName": "Kernel-Power", "Id": 41, "Level": 1, "At": 100, "Msg": "a"},
                                 {"LogName": "Application", "ProviderName": "FT", "Id": 3, "Level": 2, "At": 200, "Msg": "b"},
                                 {"LogName": "Application", "ProviderName": "FT", "Id": 3, "Level": 2, "At": 300, "Msg": "c"}]})
    got = parse_powershell(out)
    assert got["services"] == [{"name": "FTLinxGW", "display": "FactoryTalk Linx Gateway", "state": "Running", "start_mode": "Auto"}]
    summary = summarize_events(got["events"])
    assert (summary["critical"], summary["errors"]) == (1, 2)
    assert [(g["source"], g["count"], g["message"]) for g in summary["top"]] == [("Kernel-Power", 1, "a"), ("FT", 2, "c")]
    assert parse_powershell('{"services": null, "events": null}') == {"services": [], "events": []}


def test_host_and_alerts_api_in_the_demo(tmp_path):
    with TestClient(create_app(str(tmp_path), demo=True, demo_opcua=False, collect=False), headers=H) as c:
        h = c.get("/api/host").json()
        assert h["windows"] and h["disks"][0]["used_pct"] == 92.5 and h["cpu"] is not None
        assert any(s["state"] == "Stopped" and s["start_mode"] == "Auto" for s in h["services"])
        assert {"hmi.disk_low", "hmi.service_stopped", "hmi.event_critical", "hmi.event_errors"} <= {a["code"] for a in h["alerts"]}

        v = c.get("/api/alerts").json()
        got = {a["code"] for a in v["alerts"]}
        assert {"hmi.disk_low", "maintenance.overdue"} <= got and v["errors"] == {}
        assert v["counts"]["warning"] == sum(a["severity"] == "warning" for a in v["alerts"])
        assert all(a["hint"] and a["link"].get("tab") and a["since"] for a in v["alerts"])
        with zipfile.ZipFile(io.BytesIO(c.get("/api/debug/bundle").content)) as z:  # the debug bundle carries both
            info = json.loads(z.read("info.json"))
        assert info["alerts"] and info["host"]["disks"]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows event log")
def test_windows_event_log():
    from ghostmap import winservice

    ok = winevent.write(winevent.INFO, winevent.event_id("hmi", cleared=True), "Ghost Map test event (tests/test_alerts.py)")
    assert ok or not winservice.is_admin()  # an administrator (the CI runner) registers the source and writes


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell and the event log on Windows")
def test_reading_this_windows_pc(tmp_path):
    """The real PowerShell call: services (none from Rockwell on a CI runner) and the event log's errors."""
    mon = HostMonitor(tmp_path)
    asyncio.run(mon.read_windows())
    snap = mon.snapshot()
    assert snap["windows_error"] is None, snap["windows_error"]
    assert isinstance(snap["services"], list) and snap["events"]["window_h"] == 24
    assert snap["disks"] and snap["memory"]["total"] > 0 and snap["uptime_s"] > 0
    assert all(isinstance(g["count"], int) and g["source"] for g in snap["events"]["top"])


def test_refused_ixon_address_points_at_the_ixon_option():
    now = 1000.0
    ixon, other = rules.access_alerts([{"ip": "10.187.78.28", "count": 2, "first": now, "last": now, "path": "/"},
                                       {"ip": "172.17.255.50", "count": 1, "first": now, "last": now}], now)
    assert "service install --ixon" in ixon["hint"] and "IXON's VPN range" in ixon["hint"]
    assert "--allow 172.17.255.50" in other["hint"] and "VPN range" not in other["hint"]

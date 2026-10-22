"""TSC's event logs: listed newest first from both folders, read read-only, nothing outside the folders."""

import os

import pytest
from fastapi.testclient import TestClient

from ghostmap import tsclogs
from ghostmap.web.app import create_app


@pytest.fixture
def folders(tmp_path):
    new = tmp_path / "Bradbury EventLogs"
    (new / "2026").mkdir(parents=True)
    (new / "2026" / "TSC_2026-10-22.log").write_text("06:00 INFO started\n07:02 ERROR Timeout waiting for PLC\n07:03 INFO ok\n")
    old = new / "TSC_2026-10-21.log"
    old.write_text("06:00 INFO started\n")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    for user in ("Bradbury", "Operator"):  # the legacy folder is in a user's AppData: any user's
        d = tmp_path / "Users" / user / "AppData" / "Roaming" / "Bradbury" / "Event Logs"
        d.mkdir(parents=True)
    (d / "EventLog.txt").write_text("Error: Unable to open coil file\r\nDone\r\n", encoding="utf-16")
    (tmp_path / "secret.txt").write_text("not a log")
    return [("TSC event logs", str(new)),
            ("TSC event logs (legacy)", str(tmp_path / "Users" / "*" / "AppData" / "Roaming" / "Bradbury" / "Event Logs"))]


def test_lists_both_folders_newest_first(folders):
    got = tsclogs.list_files(folders)
    assert len(got["folders"]) == 3  # the new folder and two users' legacy folders
    names = [f["name"] for f in got["files"]]
    assert set(names) == {"TSC_2026-10-22.log", "TSC_2026-10-21.log", "EventLog.txt"}
    assert names[-1] == "TSC_2026-10-21.log"  # oldest last


def test_reads_tail_search_errors_and_utf16(folders):
    files = {f["name"]: f["id"] for f in tsclogs.list_files(folders)["files"]}
    r = tsclogs.read(tsclogs.resolve(files["TSC_2026-10-22.log"], folders), lines=2)
    assert r["format"] == "plain" and r["matched"] == 3  # not TSC's format: one entry per line
    assert [(e["message"], e["problem"]) for e in r["entries"]] == [("07:02 ERROR Timeout waiting for PLC", True), ("07:03 INFO ok", False)]
    assert tsclogs.read(tsclogs.resolve(files["TSC_2026-10-22.log"], folders), problems_only=True)["matched"] == 1
    assert tsclogs.read(tsclogs.resolve(files["TSC_2026-10-22.log"], folders), query="info")["matched"] == 2
    legacy = tsclogs.read(tsclogs.resolve(files["EventLog.txt"], folders))
    assert legacy["entries"][0]["message"] == "Error: Unable to open coil file" and legacy["entries"][0]["problem"]


@pytest.mark.parametrize("bad", ["0/../../secret.txt", "0/", "9/x.log", "x/y", "", "0/2026"])
def test_nothing_outside_the_log_folders(folders, bad):
    with pytest.raises(ValueError):
        tsclogs.resolve(bad, folders)


def test_large_file_reads_only_its_end(folders, tmp_path, monkeypatch):
    monkeypatch.setattr(tsclogs, "TAIL_BYTES", 100)
    p = tmp_path / "Bradbury EventLogs" / "big.log"
    p.write_text("".join(f"line {i:04d}\n" for i in range(100)))
    r = tsclogs.read(p, lines=5000)
    assert r["truncated"] and r["entries"][-1]["message"] == "line 0099" and r["matched"] < 10


def test_event_logs_page_in_demo(tmp_path):
    app = create_app(data_dir=str(tmp_path), demo=True, demo_opcua=False, collect=False)
    with TestClient(app, headers={"X-Ghostmap": "1"}) as c:
        files = c.get("/api/tsclogs").json()["files"]
        assert {f["folder"] for f in files} == {"TSC event logs", "TSC event logs (legacy)"}
        r = c.get("/api/tsclogs/file", params={"id": files[0]["id"], "problems": True}).json()
        assert r["entries"] and all(e["problem"] for e in r["entries"])
        assert c.get("/api/tsclogs/download", params={"id": files[0]["id"]}).status_code == 200
        assert c.get("/api/tsclogs/file", params={"id": "0/../../users.json"}).status_code == 404


# TSC's own format (site sample 2026-10-19, with the PC name and address replaced): one entry per line, pipe-separated,
# and lines without a timestamp (a stack trace) belonging to the entry above.
TSC_DAY = """2026-10-19 14:14:13.722|Bradbury.TSC.DataView|192.0.2.10|HMI-TEST|Error|GetUniqueColumnName|Object reference not set to an instance of an object.
2026-10-19 14:14:14.233|Bradbury.TSC.DataGridSchedule|192.0.2.10|HMI-TEST|Error|DataGridSchedule_Load|Object reference not set to an instance of an object.
System.NullReferenceException: Object reference not set to an instance of an object.
   at Bradbury.TSC.DataGridSchedule.LoadDataGridView()

2026-10-19 14:14:14.520|Bradbury.TSC.DataGridSchedule|192.0.2.10|HMI-TEST|Error|LoadDataGridView|Object reference not set to an instance of an object.
2026-10-19 14:14:14.532|Bradbury.TSC.DataGridSchedule|192.0.2.10|HMI-TEST|Error|LoadDataGridView|Object reference not set to an instance of an object.
2026-10-19 14:20:00.001|Bradbury.TSC.Service|192.0.2.10|HMI-TEST|Info|ShiftChange|Shift 2 started | crew B
"""


def test_tsc_entries_levels_multiline_and_repeats(tmp_path):
    p = tmp_path / "2026-10-19.log"
    p.write_text(TSC_DAY)
    r = tsclogs.read(p)
    assert r["format"] == "tsc" and r["total"] == 5 and r["date"] == "2026-10-19"
    assert r["levels"] == {"Error": 4, "Info": 1}
    load = r["entries"][1]
    assert load["source"] == "Bradbury.TSC.DataGridSchedule" and load["method"] == "DataGridSchedule_Load" and load["host"] == "HMI-TEST"
    assert load["more"] == ["System.NullReferenceException: Object reference not set to an instance of an object.",
                            "   at Bradbury.TSC.DataGridSchedule.LoadDataGridView()"] and load["problem"]
    assert r["entries"][-1]["message"] == "Shift 2 started | crew B" and not r["entries"][-1]["problem"]  # a | in the message stays
    assert r["top"] == [{"source": "Bradbury.TSC.DataGridSchedule", "method": "LoadDataGridView", "level": "Error", "count": 2,
                         "message": "Object reference not set to an instance of an object.",
                         "first": "2026-10-19 14:14:14.520", "last": "2026-10-19 14:14:14.532"}]
    assert tsclogs.read(p, hide=["Error"])["matched"] == 1
    assert tsclogs.read(p, source="Bradbury.TSC.DataView")["matched"] == 1
    assert tsclogs.read(p, query="nullreference")["matched"] == 1  # finds text in the folded lines
    assert sorted(f["date"] for f in [{"date": tsclogs.file_date(n)} for n in ("2026-10-19.log", "TSC_20261020.txt", "EventLog.txt")]) == ["", "2026-10-19", "2026-10-20"]

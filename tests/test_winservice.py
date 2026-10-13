"""The Windows service helpers that run anywhere: the service command line, sc.exe commands, data migration.
Installing and running the real service is checked on Windows by the exe build job."""

import subprocess
import sys

import pytest

from ghostmap import winservice as ws

SC_QUERY = """
SERVICE_NAME: GhostMap
        TYPE               : 10  WIN32_OWN_PROCESS
        STATE              : 4  RUNNING
                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)
        WIN32_EXIT_CODE    : 0  (0x0)
        SERVICE_EXIT_CODE  : 0  (0x0)
        CHECKPOINT         : 0x0
        WAIT_HINT          : 0x0
"""


def test_service_command_line_survives_spaces():
    args = ws.service_args(r"C:\ProgramData\GhostMap", "0.0.0.0", 8470, ["192.168.1.1", "10.0.0.0/24"])
    assert args[:4] == ["--data-dir", r"C:\ProgramData\GhostMap", "service", "run"]
    assert args.count("--allow") == 2 and "--no-listen" not in args
    line = ws.bin_path([r"C:\Program Files\GhostMap\GhostMap.exe"], args)
    assert line.startswith('"C:\\Program Files\\GhostMap\\GhostMap.exe" --data-dir')
    # sc.exe gets that line as one argument; Windows' own parsing gives it back unchanged.
    create = ws.sc_commands(line, exists=False)[0]
    assert create[:5] == ["sc.exe", "create", "GhostMap", "type=", "own"]
    assert create[create.index("binPath=") + 1] == line
    assert create[create.index("start=") + 1] == "delayed-auto" and create[create.index("obj=") + 1] == "LocalSystem"
    if sys.platform == "win32":
        import ctypes

        argc = ctypes.c_int()
        ctypes.windll.shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        argv = ctypes.windll.shell32.CommandLineToArgvW(subprocess.list2cmdline(create), ctypes.byref(argc))
        assert argv[create.index("binPath=") + 1] == line


def test_upgrade_repoints_the_service_and_recovery_restarts_it():
    cmds = ws.sc_commands("x", exists=True)
    assert cmds[0][:3] == ["sc.exe", "config", "GhostMap"]
    failure = next(c for c in cmds if c[1] == "failure")
    assert failure[failure.index("actions=") + 1] == "restart/10000/restart/30000/restart/60000"
    assert ["sc.exe", "failureflag", "GhostMap", "1"] in cmds
    acl = ws.acl_command(ws.shared_data_dir())
    assert "/inheritance:r" in acl and "*S-1-5-18:(OI)(CI)F" in acl and "*S-1-5-32-544:(OI)(CI)F" in acl


def test_parse_sc_query_state():
    assert ws.parse_state(SC_QUERY) == "RUNNING"
    assert ws.parse_state(SC_QUERY.replace("STATE   ", "STATUS  ").replace("4  RUNNING", "1  STOPPED")) == "STOPPED"
    assert ws.parse_state("[SC] EnumQueryServicesStatus:OpenService FAILED 1060") is None


def test_data_moves_to_the_shared_folder_once(tmp_path):
    home, shared = tmp_path / "home", tmp_path / "ProgramData" / "GhostMap"
    (home / "dashboards").mkdir(parents=True)
    (home / "dashboards" / "press.json").write_text("{}")
    (home / "users.json").write_text("{}")
    (home / "history.db").write_bytes(b"sqlite")
    (home / "logs").mkdir()
    (home / "logs" / "ghostmap.log").write_text("old log")
    (home / "x.1234.tmp").write_text("half-written")
    assert ws.migrate(home, shared) == ["dashboards", "history.db", "users.json"]
    assert (shared / "dashboards" / "press.json").exists() and not (shared / "logs").exists()
    assert (home / "users.json").exists()  # copied, not moved
    ws.write_marker(shared, {"port": 8470})
    (home / "new.json").write_text("{}")
    assert ws.migrate(home, shared) == []  # installed already: never copied over again
    other = tmp_path / "other"
    other.mkdir()
    (other / "scans").mkdir()
    assert ws.migrate(home, other) == []  # a folder with data of its own is left alone


def test_installed_data_dir_needs_the_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    assert ws.installed_data_dir() is None
    (tmp_path / "GhostMap").mkdir()
    ws.write_marker(tmp_path / "GhostMap", {})
    assert ws.installed_data_dir() == tmp_path / "GhostMap"


@pytest.mark.skipif(sys.platform == "win32", reason="on Windows this asks the real service manager")
def test_service_outside_windows_says_so():
    from ghostmap.cli import main

    with pytest.raises(SystemExit, match="only available on Windows"):
        main(["service", "status"])

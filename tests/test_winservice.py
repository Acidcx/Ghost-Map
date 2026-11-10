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


def test_purge_deletes_the_shared_data_and_the_original_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))
    home = tmp_path / "home" / ".ghostmap"
    (home / "pki").mkdir(parents=True)
    (home / "users.json").write_text("{}")
    data = tmp_path / "pd" / "GhostMap"
    (data / "tls").mkdir(parents=True)
    key = data / "tls" / "ghostmap.key"
    key.write_text("secret")
    key.chmod(0o400)  # read-only files don't stop it
    targets = ws.purge_targets(home)
    assert targets == [data, home]
    assert all(ws.remove_tree(p) for p in targets)
    assert not data.exists() and not home.exists()
    assert ws.purge_targets(home) == []  # nothing left, and running it again is fine


def test_purge_removes_the_firewall_rules_for_the_installed_exe():
    exe = r"C:\Program Files\GhostMap\GhostMap.exe"
    cmd = ws.firewall_delete_command(exe)
    assert cmd[:5] == ["netsh", "advfirewall", "firewall", "delete", "rule"]
    assert "name=all" in cmd and f"program={exe}" in cmd


def test_uninstall_purge_is_an_option():
    from ghostmap.cli import build_parser

    assert build_parser().parse_args(["service", "uninstall", "--purge"]).purge


def test_install_keeps_the_firewall_rule_in_line_with_allow():
    exe = r"C:\Program Files\GhostMap\GhostMap.exe"
    delete, add = ws.firewall_commands(exe, 8470, "0.0.0.0", ["10.187.0.0/16", "172.17.255.200"])
    assert delete == ws.firewall_delete_command(exe)  # replaces any earlier rule, one added by hand included
    assert "remoteip=10.187.0.0/16,172.17.255.200" in add and "localport=8470" in add and f"program={exe}" in add
    assert ws.firewall_commands(exe, 8470, "127.0.0.1", []) == [delete]  # local only: no rule left open


def test_ixon_option_allows_the_whole_vpn_range(monkeypatch):
    """IXON's web access came from 10.187.202.89, then 10.187.78.28 after an IXrouter reboot (Test HMI, 2026-10)."""
    from ghostmap import cli
    from ghostmap.web.auth import client_allowed, parse_allow

    seen = {}
    monkeypatch.setattr(ws, "install", lambda host, port, allow, *a, **k: seen.update(host=host, allow=list(allow)) or 0)
    assert cli.main(["service", "install", "--ixon", "--allow", "172.17.255.200"]) == 0
    assert seen["host"] == "0.0.0.0" and seen["allow"] == ["172.17.255.200", "10.187.0.0/16"]
    nets = parse_allow(seen["allow"])
    assert client_allowed("10.187.202.89", nets) and client_allowed("10.187.78.28", nets)
    assert not client_allowed("10.188.0.1", nets)


def test_upgrade_with_no_options_keeps_ixon_access(tmp_path, monkeypatch):
    """Running install from a newer exe used to fall back to localhost only, cutting off IXON until someone noticed."""
    from ghostmap import cli

    monkeypatch.setenv("ProgramData", str(tmp_path))
    (tmp_path / "GhostMap").mkdir()
    ws.write_marker(tmp_path / "GhostMap", {"host": "0.0.0.0", "port": 8470, "allow": ["10.187.0.0/16"], "https": True})
    seen = {}
    monkeypatch.setattr(ws, "install", lambda host, port, allow, *a, **k: seen.update(host=host, allow=list(allow),
                                                                                       https=k["https"]) or 0)
    assert cli.main(["service", "install"]) == 0
    assert seen == {"host": "0.0.0.0", "allow": ["10.187.0.0/16"], "https": True}
    assert cli.main(["service", "install", "--host", "127.0.0.1"]) == 0  # asked for local only: gets it
    assert seen["host"] == "127.0.0.1" and seen["allow"] == []


def test_status_names_the_installed_version(monkeypatch):
    import ghostmap

    monkeypatch.setattr(ghostmap, "BUILD", {"commit": "abc1234567"})
    same = {"version": ghostmap.__version__, "commit": "abc1234567", "installed_at": "2026-10-29 08:00:00"}
    assert ws.version_line(same) == f"Version: {ghostmap.__version__} (abc1234567), installed 2026-10-29 08:00:00"
    older = ws.version_line({**same, "version": "0.4.9", "commit": "0000000"})
    assert older.startswith("Version: 0.4.9 (0000000)") and f"This exe is {ghostmap.__version__} (abc1234567)" in older
    assert "before 0.5.0" in ws.version_line({"installed_at": "2026-10-01 08:00:00"})


def test_network_service_gets_https_by_default(tmp_path, monkeypatch, capsys):
    """0.5.2: a service open to the network serves HTTPS unless install says --http, also on an upgrade."""
    from ghostmap import cli

    monkeypatch.setenv("ProgramData", str(tmp_path))
    (tmp_path / "GhostMap").mkdir()
    seen = {}
    monkeypatch.setattr(ws, "install", lambda host, port, allow, *a, **k: seen.update(host=host, https=k["https"]) or 0)
    assert cli.main(["service", "install", "--ixon"]) == 0
    assert seen == {"host": "0.0.0.0", "https": True}
    assert cli.main(["service", "install", "--ixon", "--http"]) == 0
    assert seen == {"host": "0.0.0.0", "https": False}
    assert cli.main(["service", "install"]) == 0  # local only: plain HTTP for the HMI's own browser
    assert seen == {"host": "127.0.0.1", "https": False}
    # Upgrade of a service a 0.5.1 install opened to IXON over plain HTTP.
    ws.write_marker(tmp_path / "GhostMap", {"host": "0.0.0.0", "port": 8470, "allow": ["10.187.0.0/16"], "https": False})
    capsys.readouterr()
    assert cli.main(["service", "install"]) == 0
    assert seen == {"host": "0.0.0.0", "https": True} and "IXON" in capsys.readouterr().err
    assert cli.main(["service", "install", "--http"]) == 0
    assert seen == {"host": "0.0.0.0", "https": False}


def test_service_args_record_plain_http_choice(tmp_path):
    assert "--http" in ws.service_args(tmp_path, "0.0.0.0", 8470, ["10.187.0.0/16"])
    assert "--http" not in ws.service_args(tmp_path, "0.0.0.0", 8470, ["10.187.0.0/16"], https=True)
    assert "--http" not in ws.service_args(tmp_path, "127.0.0.1", 8470)


def _fake_build(root, unpacked):
    root.mkdir(parents=True)
    (root / "GhostMap.exe").write_bytes(b"exe-" + root.name.encode())
    if unpacked:
        (root / ws.INTERNAL / "lib").mkdir(parents=True)
        (root / ws.INTERNAL / "lib" / "new.pyd").write_bytes(b"new")
    return root / "GhostMap.exe"


def test_unpacked_build_is_copied_with_its_libraries_and_replaces_stale_ones(tmp_path):
    target = tmp_path / "Program Files" / "GhostMap"
    (target / ws.INTERNAL).mkdir(parents=True)
    (target / ws.INTERNAL / "old.pyd").write_bytes(b"old")  # from an earlier version
    exe = _fake_build(tmp_path / "download", unpacked=True)
    assert ws.copy_program(exe, target, unpacked=True) == target / "GhostMap.exe"
    assert (target / "GhostMap.exe").read_bytes() == b"exe-download"
    assert (target / ws.INTERNAL / "lib" / "new.pyd").exists()
    assert not (target / ws.INTERNAL / "old.pyd").exists()


def test_single_file_exe_over_an_unpacked_install_removes_its_libraries(tmp_path):
    target = tmp_path / "GhostMap"
    _fake_build(target, unpacked=True)
    exe = _fake_build(tmp_path / "onefile", unpacked=False)
    ws.copy_program(exe, target, unpacked=False)
    assert (target / "GhostMap.exe").read_bytes() == b"exe-onefile"
    assert not (target / ws.INTERNAL).exists()


def test_setup_installed_program_is_used_where_it_is(tmp_path):
    target = tmp_path / "GhostMap"
    exe = _fake_build(target, unpacked=True)
    assert ws.copy_program(exe, target, unpacked=True) == exe
    assert (target / ws.INTERNAL / "lib" / "new.pyd").exists()


def test_plain_python_is_not_the_unpacked_build():
    assert ws.is_unpacked() is False

import json

from ghostmap import cli
from ghostmap.models import to_dict
from ghostmap.sim.machine import build_demo_scan


def test_no_arguments_starts_web_ui_and_opens_browser(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.sys, "argv", ["GhostMap.exe"])
    monkeypatch.setattr(cli, "cmd_web", lambda args: seen.update(vars(args)) or 0)
    assert cli.main() == 0
    assert seen["cmd"] == "web" and seen["open"] is True and seen["port"] == 8470


def test_diff_command_on_files(tmp_path, capsys):
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text(json.dumps(to_dict(build_demo_scan("baseline"))))
    new.write_text(json.dumps(to_dict(build_demo_scan("today"))))
    assert cli.main(["diff", str(old), str(new)]) == 0
    out = capsys.readouterr().out
    assert "Replaced PowerFlex 525 @ 192.168.1.22" in out

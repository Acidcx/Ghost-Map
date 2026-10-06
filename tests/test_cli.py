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


def test_user_commands(tmp_path, monkeypatch, capsys):
    from ghostmap.web import auth

    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    monkeypatch.setenv("GHOSTMAP_PASSWORD", "long-enough-pw")
    d = str(tmp_path)
    assert cli.main(["--data-dir", d, "user", "add", "boss", "--role", "admin"]) == 0
    assert cli.main(["--data-dir", d, "user", "list"]) == 0
    assert "boss\tadmin" in capsys.readouterr().out
    assert auth.UserStore(tmp_path).verify("boss", "long-enough-pw") == "admin"
    monkeypatch.setenv("GHOSTMAP_PASSWORD", "short")
    assert cli.main(["--data-dir", d, "user", "add", "x"]) == 1
    assert cli.main(["--data-dir", d, "user", "remove", "boss"]) == 0
    assert cli.main(["--data-dir", d, "user", "remove", "boss"]) == 1


def test_web_refuses_network_exposure_without_allow_list_and_logins(tmp_path, monkeypatch, capsys):
    import uvicorn

    from ghostmap.web import auth

    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    d = str(tmp_path)
    base = ["--data-dir", d, "web", "--host", "0.0.0.0", "--port", "18471"]
    assert cli.main(base) == 2
    assert "--allow" in capsys.readouterr().err
    assert cli.main(base + ["--allow", "192.168.1.1"]) == 2
    assert "user add" in capsys.readouterr().err
    assert cli.main(base + ["--allow", "bogus"]) == 2
    auth.UserStore(tmp_path).set("boss", "long-enough-pw", "admin")
    assert cli.main(base + ["--allow", "192.168.1.1"]) == 0

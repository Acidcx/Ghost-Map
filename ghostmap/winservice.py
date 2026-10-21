"""Ghost Map as a Windows service, so it runs on the HMI from boot whether or not anyone is logged on.

``GhostMap.exe service install`` (from an elevated prompt):

- copies the exe to ``C:\\Program Files\\GhostMap`` (only administrators can replace it there; a service running
  as LocalSystem must not start an exe that any user can overwrite),
- moves the data to ``C:\\ProgramData\\GhostMap``: the installing user's ``~/.ghostmap`` is copied there the first
  time, and the folder is limited to SYSTEM and Administrators (it holds the logins and saved passwords),
- registers the service ``GhostMap`` (LocalSystem, delayed automatic start so the network and FactoryTalk
  services come up first), restarted by Windows 10 s, 30 s and 60 s after a crash or an unexpected stop,
- and starts it.

Running ``service install`` again from a newer exe upgrades in place: the service is stopped, the exe replaced
and the service started again, with the data left as it is. ``service uninstall`` removes the service and
leaves the data; ``service uninstall --purge`` also deletes the data (logins, saved passwords, certificates,
history), the installing user's ``~/.ghostmap``, the event log source and any firewall rule for the installed exe. The service itself is ``GhostMap.exe --data-dir C:\\ProgramData\\GhostMap service run ...``,
which hands the process to the service manager (``run_as_service``).

The helpers that only build commands or copy files work on any OS (and are tested everywhere); the rest needs
Windows.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

SERVICE_NAME = "GhostMap"
DISPLAY_NAME = "Ghost Map"
DESCRIPTION = ("Read-only OT network mapper and machine health. Web UI on this PC at http://127.0.0.1:8470. "
               "Reads only: never writes to a PLC, switch or gateway.")
MARKER = "service.json"          # in the shared data folder: written by install, read by default_data_dir()
EXE_NAME = "GhostMap.exe"
RESTART_DELAYS_MS = (10_000, 30_000, 60_000)  # after the 1st, 2nd and later failures
RESET_FAILURES_S = 86_400         # the failure count goes back to 0 after a day without one
NOT_COPIED = {"logs", MARKER}     # left behind when the data moves to the shared folder
log = logging.getLogger("ghostmap.service")


def shared_data_dir() -> Path:
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "GhostMap"


def install_dir() -> Path:
    return Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "GhostMap"


def installed_data_dir() -> Optional[Path]:
    """The shared folder when the service is installed and this user can see it (administrators), else None."""
    d = shared_data_dir()
    return d if os.path.exists(d / MARKER) else None  # os.path.exists: False, not an error, when access is denied


def installed_info() -> Optional[dict]:
    """What ``service install`` recorded (host, port, https, ...), when the service is installed and visible."""
    d = installed_data_dir()
    if d is None:
        return None
    try:
        return json.loads((d / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def service_args(data_dir: Path, host: str, port: int, allow: Sequence[str] = (), no_listen: bool = False,
                 https: bool = False, cert: Optional[str] = None, key: Optional[str] = None) -> list[str]:
    """The arguments the service is started with."""
    args = ["--data-dir", str(data_dir), "service", "run", "--host", host, "--port", str(port)]
    for a in allow:
        args += ["--allow", str(a)]
    if no_listen:
        args.append("--no-listen")
    if https:
        args.append("--https")
        if cert and key:
            args += ["--cert", str(Path(cert).resolve()), "--key", str(Path(key).resolve())]
    return args


def bin_path(program: Sequence[str], args: Sequence[str]) -> str:
    """The service's command line: the program quoted, so a path with spaces works."""
    return subprocess.list2cmdline([*program, *args])


def sc_commands(binpath: str, exists: bool) -> list[list[str]]:
    """sc.exe commands that register (or re-point) the service and set its recovery."""
    first = (["sc.exe", "config", SERVICE_NAME] if exists
             else ["sc.exe", "create", SERVICE_NAME, "type=", "own"])
    actions = "/".join(f"restart/{ms}" for ms in RESTART_DELAYS_MS)
    return [
        first + ["binPath=", binpath, "start=", "delayed-auto", "obj=", "LocalSystem", "DisplayName=", DISPLAY_NAME],
        ["sc.exe", "description", SERVICE_NAME, DESCRIPTION],
        ["sc.exe", "failure", SERVICE_NAME, "reset=", str(RESET_FAILURES_S), "actions=", actions],
        ["sc.exe", "failureflag", SERVICE_NAME, "1"],  # also restart after a stop with an error, not only a crash
    ]


def acl_command(folder: Path) -> list[str]:
    """Limit the data folder to SYSTEM and Administrators (by SID, so it works on any language of Windows)."""
    return ["icacls", str(folder), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]


def migrate(src: Path, dst: Path) -> list[str]:
    """Copy an existing data folder into the shared one, the first time only. Returns what was copied."""
    if not src.is_dir() or src.resolve() == dst.resolve() or (dst / MARKER).exists():
        return []
    dst.mkdir(parents=True, exist_ok=True)
    if any(p.name not in NOT_COPIED for p in dst.iterdir()):
        return []  # the shared folder already has data of its own: don't mix the two
    copied = []
    for p in sorted(src.iterdir()):
        if p.name in NOT_COPIED or p.name.endswith(".tmp"):
            continue
        if p.is_dir():
            shutil.copytree(p, dst / p.name, dirs_exist_ok=True)
        else:
            shutil.copy2(p, dst / p.name)
        copied.append(p.name)
    return copied


def write_marker(data_dir: Path, info: dict) -> None:
    (data_dir / MARKER).write_text(json.dumps(info, indent=1), encoding="utf-8")


_STATES = {1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING", 4: "RUNNING", 5: "CONTINUE_PENDING",
           6: "PAUSE_PENDING", 7: "PAUSED"}
_STATE_LINE = re.compile(r":\s*([1-7])\s+[A-Z_]+\s*$")


def parse_state(sc_query_output: str) -> Optional[str]:
    """RUNNING, STOPPED, START_PENDING... from ``sc query`` output, or None if the service isn't there.

    By the state number rather than the "STATE" label, which some languages of Windows translate."""
    for line in sc_query_output.splitlines():
        m = _STATE_LINE.search(line)
        if m:
            return _STATES[int(m.group(1))]
    return None


# ------------------------------------------------------------------------------------------- Windows only
def _need_windows() -> None:
    if sys.platform != "win32":
        raise SystemExit("error: the Windows service is only available on Windows")


def is_admin() -> bool:  # pragma: no cover - Windows only
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _sc(*args: str, check: bool = True) -> subprocess.CompletedProcess:  # pragma: no cover - Windows only
    r = subprocess.run(list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        out = (r.stdout + r.stderr).strip().splitlines()
        raise SystemExit(f"error: {' '.join(args[:3])} failed: {out[-1] if out else r.returncode}")
    return r


def state() -> Optional[str]:  # pragma: no cover - Windows only
    r = _sc("sc.exe", "query", SERVICE_NAME, check=False)
    return parse_state(r.stdout) if r.returncode == 0 else None


def wait_for_state(want: str, timeout: float = 60) -> bool:  # pragma: no cover - Windows only
    end = time.time() + timeout
    while time.time() < end:
        if state() == want:
            return True
        time.sleep(0.5)
    return False


def program() -> list[str]:
    """How to start this Ghost Map: the exe when it is the packaged one, else this Python with ``-m ghostmap``."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "ghostmap"]


def install(host: str, port: int, allow: Sequence[str], no_listen: bool, check_exposure: Callable[[Path], Optional[str]],
            out=print, https: bool = False, cert: Optional[str] = None, key: Optional[str] = None) -> int:  # pragma: no cover - Windows only (run by the Windows CI job)
    _need_windows()
    if not is_admin():
        out("error: installing a service needs an elevated prompt: right-click Command Prompt, Run as administrator")
        return 2
    data = shared_data_dir()
    current = state()
    if current is None and _port_busy(port):
        out(f"error: something is already listening on port {port} (Ghost Map running in a window?). Close it first, "
            "so its data can be copied while nothing is writing to it.")
        return 2
    home = Path(os.environ.get("GHOSTMAP_DATA") or Path.home() / ".ghostmap")
    copied = migrate(home, data)
    data.mkdir(parents=True, exist_ok=True)
    problem = check_exposure(data)
    if problem:
        out(problem)
        return 2
    if current is not None and current != "STOPPED":
        out("Stopping the running service to upgrade it...")
        _sc("sc.exe", "stop", SERVICE_NAME, check=False)
        if not wait_for_state("STOPPED", 60):
            out("error: the service did not stop within 60 s")
            return 1
    prog = program()
    if getattr(sys, "frozen", False):
        target = install_dir() / EXE_NAME
        if Path(sys.executable).resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            for i in range(20):  # the stopped service's process can take a moment to let go of the file
                try:
                    shutil.copy2(sys.executable, target)
                    break
                except PermissionError:
                    if i == 19:
                        raise
                    time.sleep(0.5)
        prog = [str(target)]
    else:
        out(f"note: the service will run this Python ({sys.executable}). Install GhostMap.exe for a plant PC.")
    tls_info = None
    if https:  # made now (as an administrator) so the fingerprint can be shown; the service reuses it
        from ghostmap import tls

        try:
            tls_info = tls.ensure(data, cert, key)
        except ValueError as exc:
            out(f"error: {exc}")
            return 2
    binpath = bin_path(prog, service_args(data, host, port, allow, no_listen, https, cert, key))
    for cmd in sc_commands(binpath, exists=current is not None):
        _sc(*cmd)
    if getattr(sys, "frozen", False):
        delete, *add = firewall_commands(Path(prog[0]), port, host, allow)
        _sc(*delete, check=False)  # fails harmlessly when there is no rule yet
        if add and _sc(*add[0], check=False).returncode != 0:
            out("warning: could not add the Windows Firewall rule; add an inbound rule for TCP "
                f"{port} from {', '.join(map(str, allow))} by hand")
        elif add:
            out(f"Windows Firewall: TCP {port} open to {', '.join(map(str, allow))} (rule '{FIREWALL_RULE}')")
    _sc(*acl_command(data))
    from ghostmap import winevent

    winevent.register()  # alerts in the Application event log under "GhostMap"
    write_marker(data, {"installed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "program": prog, "bin_path": binpath,
                        "host": host, "port": port, "allow": list(allow), "https": https, "copied_from_home": copied})
    _sc("sc.exe", "start", SERVICE_NAME)
    if not wait_for_state("RUNNING", 60):
        out("error: the service did not start; see the System event log and " + str(data / "logs" / "ghostmap.log"))
        return 1
    url = f"{'https' if https else 'http'}://127.0.0.1:{port}"
    up = _wait_http(f"{url}/api/info", 60, tls_info["cert"] if tls_info else None)
    if copied:
        out(f"Copied {', '.join(copied)} from {home} (the originals are left as they were).")
    out(f"Ghost Map is {'running' if up else 'starting'} as the Windows service '{SERVICE_NAME}' at {url}")
    out(f"Data: {data}   Program: {prog[0]}")
    if tls_info:
        out(f"HTTPS certificate: {tls_info['cert']}\n  SHA-256 {tls_info['sha256']} (check it against what the browser shows)")
    out("It starts with Windows and is restarted if it stops unexpectedly. "
        "Stop or remove it with: GhostMap.exe service stop | service uninstall")
    return 0 if up else 1


def purge_targets(home: Path) -> list[Path]:
    """The data folders ``uninstall --purge`` deletes: the shared one and the installing user's own (install copies
    it and leaves the original)."""
    targets = []
    for p in (shared_data_dir(), home):
        if p.is_dir() and all(p.resolve() != t.resolve() for t in targets):
            targets.append(p)
    return targets


def firewall_delete_command(exe: Path) -> list[str]:
    """Delete every Windows Firewall rule for this program, whatever it was named (the IXON setup guide adds one)."""
    return ["netsh", "advfirewall", "firewall", "delete", "rule", "name=all", f"program={exe}"]


FIREWALL_RULE = "Ghost Map"


def firewall_commands(exe: Path, port: int, host: str, allow: Sequence[str]) -> list[list[str]]:
    """Make the inbound firewall rule match what Ghost Map serves: every earlier rule for the exe goes (including one
    added by hand), and one rule for ``port`` from the ``--allow`` addresses comes back when it listens beyond this
    PC. A service gets no firewall prompt, so without the rule remote clients just time out."""
    cmds = [firewall_delete_command(exe)]
    if host not in ("127.0.0.1", "localhost", "::1") and allow:
        cmds.append(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}", "dir=in",
                     "action=allow", "protocol=TCP", f"localport={port}", f"remoteip={','.join(map(str, allow))}",
                     f"program={exe}", "enable=yes"])
    return cmds


def remove_tree(path: Path, tries: int = 20) -> bool:
    """Delete a folder, clearing read-only flags, and retrying while a just-stopped service lets go of its files."""
    def unlock(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)

    handler = {"onexc": unlock} if sys.version_info >= (3, 12) else {"onerror": unlock}
    for i in range(tries):
        try:
            shutil.rmtree(path, **handler)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if i == tries - 1:
                return False
            time.sleep(0.5)
    return False


def uninstall(out=print, purge: bool = False) -> int:  # pragma: no cover - Windows only
    _need_windows()
    if not is_admin():
        out("error: removing a service needs an elevated prompt: right-click Command Prompt, Run as administrator")
        return 2
    installed = state() is not None
    if installed:
        _sc("sc.exe", "stop", SERVICE_NAME, check=False)
        wait_for_state("STOPPED", 60)
        _sc("sc.exe", "delete", SERVICE_NAME)
    elif not purge:
        out(f"The service '{SERVICE_NAME}' is not installed.")
        return 0
    data = shared_data_dir()
    exe = install_dir() / EXE_NAME
    if purge:
        _sc(*firewall_delete_command(exe), check=False)
        from ghostmap import winevent

        winevent.unregister()
        home = Path(os.environ.get("GHOSTMAP_DATA") or Path.home() / ".ghostmap")
        failed = [str(p) for p in purge_targets(home) if not remove_tree(p)]
    else:
        try:
            (data / MARKER).unlink()
        except OSError:
            pass
    if exe.exists() and Path(sys.executable).resolve() != exe.resolve():
        try:
            exe.unlink()
            exe.parent.rmdir()
        except OSError:
            pass
    if not purge:
        out(f"Removed the service. The data is still in {data} (use --data-dir to open it from a window).")
        return 0
    out(("Removed the service. " if installed else "")
        + "Deleted the data, logins, saved passwords and certificates, the event log source and the firewall rules "
          "for Ghost Map.")
    if exe.exists():
        out(f"Delete {exe.parent} yourself: this exe is running from there.")
    out("Also delete the GhostMap.exe you ran this from, and any copy in Downloads or on the desktop.")
    if failed:
        out("error: could not delete " + ", ".join(failed) + " (still in use?). Restart Windows and run this again.")
        return 1
    return 0


def control(action: str, out=print) -> int:  # pragma: no cover - Windows only
    _need_windows()
    if action == "status":
        st = state()
        if st is None:
            out(f"The service '{SERVICE_NAME}' is not installed.")
            return 1
        info = {}
        try:
            info = json.loads((shared_data_dir() / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        out(f"{SERVICE_NAME}: {st}" + (f" at http://127.0.0.1:{info['port']}" if info.get("port") else ""))
        if info:
            out(f"Program: {info.get('program', ['?'])[0]}   Data: {shared_data_dir()}")
        return 0 if st == "RUNNING" else 1
    if not is_admin():
        out(f"error: {action}ing the service needs an elevated prompt: right-click Command Prompt, Run as administrator")
        return 2
    _sc("sc.exe", action, SERVICE_NAME, check=False)
    ok = wait_for_state("RUNNING" if action == "start" else "STOPPED", 60)
    out(f"{SERVICE_NAME}: {state()}")
    return 0 if ok else 1


def _port_busy(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_http(url: str, timeout: float, cafile: Optional[str] = None) -> bool:  # pragma: no cover - Windows only
    import ssl
    import urllib.request

    ctx = None
    if cafile:  # trust exactly the certificate the service was given (it may not name 127.0.0.1)
        ctx = ssl.create_default_context(cafile=cafile)
        ctx.check_hostname = False
        ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2, context=ctx) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ------------------------------------------------------------------------- the service process (Windows)
def run_as_service(make_server: Callable[[], object]) -> int:  # pragma: no cover - Windows only (CI exe smoke test)
    """Hand this process to the service manager and run ``make_server()`` (a uvicorn.Server) until it says stop.

    Returns when the service has stopped. If this process was not started by the service manager, says so.
    """
    _need_windows()
    import ctypes
    from ctypes import wintypes

    SERVICE_WIN32_OWN_PROCESS = 0x10
    STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
    ACCEPT_STOP, ACCEPT_SHUTDOWN = 0x1, 0x4
    CONTROL_STOP, CONTROL_INTERROGATE, CONTROL_SHUTDOWN = 1, 4, 5
    NO_ERROR, ERROR_CALL_NOT_IMPLEMENTED, ERROR_SERVICE_SPECIFIC_ERROR = 0, 120, 1066
    ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    MAIN = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
    HANDLER = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", MAIN)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]
    advapi.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
    advapi.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER, wintypes.LPVOID]
    advapi.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    advapi.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
    advapi.SetServiceStatus.restype = wintypes.BOOL

    st = {"handle": None, "server": None, "stopping": False, "checkpoint": 0, "code": 0}

    def report(state: int, exit_code: int = NO_ERROR, specific: int = 0, wait_ms: int = 0) -> None:
        if not st["handle"]:
            return
        pending = state in (START_PENDING, STOP_PENDING)
        st["checkpoint"] = st["checkpoint"] + 1 if pending else 0
        s = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state,
                           0 if state == START_PENDING else ACCEPT_STOP | ACCEPT_SHUTDOWN,
                           exit_code, specific, st["checkpoint"], wait_ms)
        advapi.SetServiceStatus(st["handle"], ctypes.byref(s))

    def handler(control, _event, _data, _ctx):
        if control in (CONTROL_STOP, CONTROL_SHUTDOWN):
            log.info("service: stop requested by Windows")
            st["stopping"] = True
            report(STOP_PENDING, wait_ms=30_000)
            if st["server"] is not None:
                st["server"].should_exit = True
            return NO_ERROR
        if control == CONTROL_INTERROGATE:
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    handler_cb = HANDLER(handler)  # kept referenced for the life of the service

    def service_main(_argc, _argv):
        st["handle"] = advapi.RegisterServiceCtrlHandlerExW(SERVICE_NAME, handler_cb, None)
        if not st["handle"]:
            return
        report(START_PENDING, wait_ms=60_000)
        failed = False
        try:
            st["server"] = make_server()
            report(RUNNING)
            log.info("service: running")
            st["server"].run()
        except BaseException:  # uvicorn exits with SystemExit when it can't bind the port
            log.exception("service: Ghost Map stopped with an error")
            failed = True
        failed = failed or not st["stopping"]  # stopped without being asked: let Windows restart it
        if failed:
            log.error("service: stopped unexpectedly; Windows will restart it")
        st["code"] = 1 if failed else 0
        report(STOPPED, ERROR_SERVICE_SPECIFIC_ERROR if failed else NO_ERROR, 1 if failed else 0)

    main_cb = MAIN(service_main)
    table = (SERVICE_TABLE_ENTRYW * 2)(SERVICE_TABLE_ENTRYW(SERVICE_NAME, main_cb), SERVICE_TABLE_ENTRYW(None, MAIN()))
    if not advapi.StartServiceCtrlDispatcherW(table):
        err = ctypes.get_last_error()
        if err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            print("'service run' is started by Windows' service manager. To install the service, run "
                  "'GhostMap.exe service install' from an elevated prompt; to run in this window, 'GhostMap.exe web'.",
                  file=sys.stderr)
            return 2
        raise OSError(err, f"StartServiceCtrlDispatcher failed ({err})")
    return st["code"]

"""The health of the PC Ghost Map runs on (the HMI): disk, memory, CPU, uptime, and on Windows the FactoryTalk
and Rockwell services and the errors in the Windows event log.

Read-only like everything else: it looks at the PC, never starts, stops or changes anything. Disk, memory, CPU
and uptime come from the OS directly (every ``FAST_S``). Services and the event log come from one PowerShell
call made of ``Get-`` commands only (every ``SLOW_S``), so a PC where PowerShell is locked down still shows the
rest. ``analysis/alerts.py`` turns a snapshot into alerts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

FAST_S = 60.0          # disk, memory, CPU, uptime
SLOW_S = 300.0         # services and event log (one PowerShell call)
FIRST_SLOW_S = 20.0    # ... the first time this long after starting, so starting up stays quick
CPU_KEPT = 15          # one-minute CPU samples kept (the CPU alert looks at the average)
EVENT_WINDOW_H = 24
PS_TIMEOUT_S = 90
# Services whose display name matches are listed (FactoryTalk Linx, Linx Gateway, View SE, Activation, ...).
SERVICE_MATCH = "FactoryTalk|Rockwell|RSLinx|RSView|FT Linx"
log = logging.getLogger("ghostmap.host")

PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$svc = @(Get-CimInstance Win32_Service | Where-Object { $_.DisplayName -match '%MATCH%' } |
  Select-Object Name, DisplayName, State, StartMode)
$since = (Get-Date).AddHours(-%HOURS%)
$ev = @(Get-WinEvent -FilterHashtable @{LogName='System','Application'; Level=1,2; StartTime=$since} -MaxEvents 1000 |
  Select-Object LogName, ProviderName, Id, Level,
    @{n='At'; e={([DateTimeOffset]$_.TimeCreated).ToUnixTimeSeconds()}},
    @{n='Msg'; e={ if ($_.Message) { ($_.Message -split "`r?`n")[0] } else { '' } }})
@{services=$svc; events=$ev} | ConvertTo-Json -Depth 4 -Compress
"""


def disks(data_dir: Path) -> list[dict]:
    """The drive Ghost Map's data is on, and the system drive (once if they are the same)."""
    paths = [Path(data_dir)]
    paths.append(Path(os.environ.get("SystemDrive", "C:") + "\\") if sys.platform == "win32" else Path("/"))
    out, seen = [], set()
    for p in paths:
        probe = p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            u = shutil.disk_usage(probe)
        except OSError:
            continue
        key = (u.total, u.used)
        if key in seen:
            out[[d["key"] for d in out].index(key)]["data"] = True
            continue
        seen.add(key)
        drive = probe.anchor if sys.platform == "win32" else str(probe)
        out.append({"key": key, "path": drive.rstrip("\\") or drive, "total": u.total, "free": u.free,
                    "used_pct": round(u.used / u.total * 100, 1) if u.total else None, "data": p == paths[0]})
    for d in out:
        d.pop("key")
    return out


def memory() -> Optional[dict]:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        total, avail = m.ullTotalPhys, m.ullAvailPhys
    else:
        try:
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024
            total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        except (OSError, KeyError, ValueError, IndexError):
            return None
    return {"total": total, "available": avail, "used_pct": round((total - avail) / total * 100, 1) if total else None}


def uptime_s() -> Optional[float]:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return ctypes.windll.kernel32.GetTickCount64() / 1000
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cpu_times() -> Optional[tuple[float, float]]:
    """(busy, total) CPU time since boot, in any unit; two readings give the load between them."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def n(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        total = n(kernel) + n(user)  # kernel time includes idle time
        return float(total - n(idle)), float(total)
    try:
        f = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        idle = f[3] + (f[4] if len(f) > 4 else 0)
        return sum(f) - idle, sum(f)
    except (OSError, ValueError, IndexError):
        return None


def parse_powershell(out: str) -> dict:
    """Services and event-log errors from the PowerShell call's JSON."""
    data = json.loads(out or "{}")

    def listify(x):
        return [] if x is None else x if isinstance(x, list) else [x]

    services = [{"name": s.get("Name"), "display": s.get("DisplayName"), "state": s.get("State"),
                 "start_mode": s.get("StartMode")} for s in listify(data.get("services"))]
    events = [{"log": e.get("LogName"), "source": e.get("ProviderName"), "id": e.get("Id"),
               "level": "critical" if e.get("Level") == 1 else "error", "at": e.get("At"), "message": e.get("Msg") or ""}
              for e in listify(data.get("events"))]
    return {"services": sorted(services, key=lambda s: (s["display"] or "").lower()), "events": events}


def summarize_events(events: list[dict], top: int = 8) -> dict:
    """Counts by level, and the sources with the most errors (each with its latest message)."""
    by: dict[tuple, dict] = {}
    for e in events:
        k = (e["log"], e["source"], e["id"])
        g = by.setdefault(k, {"log": e["log"], "source": e["source"], "id": e["id"], "level": e["level"], "count": 0,
                              "last": 0, "message": ""})
        g["count"] += 1
        if e["level"] == "critical":
            g["level"] = "critical"
        if (e["at"] or 0) >= g["last"]:
            g["last"], g["message"] = e["at"] or 0, e["message"]
    groups = sorted(by.values(), key=lambda g: (g["level"] != "critical", -g["count"], -g["last"]))
    return {"window_h": EVENT_WINDOW_H, "errors": sum(1 for e in events if e["level"] == "error"),
            "critical": sum(1 for e in events if e["level"] == "critical"), "top": groups[:top]}


def run_powershell(script: str, timeout: float = PS_TIMEOUT_S, running: Optional[set] = None) -> str:  # pragma: no cover - Windows only
    """``running``: the process is kept there while it runs, so stopping Ghost Map can end it."""
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    p = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=0x08000000)  # CREATE_NO_WINDOW
    if running is not None:
        running.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    finally:
        if running is not None:
            running.discard(p)
    if p.returncode != 0 and not out.strip():
        raise RuntimeError((err.strip().splitlines() or [f"exit code {p.returncode}"])[-1])
    return out


class HostMonitor:
    def __init__(self, data_dir: Path, demo: bool = False):
        self.data_dir = Path(data_dir)
        self.demo = demo
        self.cpu: deque = deque(maxlen=CPU_KEPT)  # (time, percent)
        self._last_cpu = cpu_times()
        self.fast: dict = {}
        self.slow: dict = {"at": None, "services": None, "events": None, "error": None}
        self._task: Optional[asyncio.Task] = None
        self._running: set = set()  # a PowerShell process while one runs

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="ghostmap-host")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            for p in list(self._running):
                p.kill()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        last_slow = time.time() - SLOW_S + FIRST_SLOW_S
        while True:
            try:
                self.sample()
                if time.time() - last_slow >= SLOW_S:
                    last_slow = time.time()
                    await self.read_windows()
            except Exception:
                log.exception("host health check failed")
            await asyncio.sleep(FAST_S)

    def sample(self) -> dict:
        now = time.time()
        cur = cpu_times()
        if cur and self._last_cpu and cur[1] > self._last_cpu[1]:
            pct = (cur[0] - self._last_cpu[0]) / (cur[1] - self._last_cpu[1]) * 100
            self.cpu.append((now, round(max(0.0, min(pct, 100.0)), 1)))
        self._last_cpu = cur or self._last_cpu
        up = uptime_s()
        self.fast = {"at": now, "disks": disks(self.data_dir), "memory": memory(), "uptime_s": up,
                     "boot_at": now - up if up else None}
        return self.fast

    async def read_windows(self) -> None:
        if self.demo:
            self.slow = {"at": time.time(), "error": None, **_demo_windows()}
            return
        if sys.platform != "win32":
            return
        script = PS_SCRIPT.replace("%MATCH%", SERVICE_MATCH).replace("%HOURS%", str(EVENT_WINDOW_H))
        try:
            got = parse_powershell(await asyncio.to_thread(run_powershell, script, PS_TIMEOUT_S, self._running))
            self.slow = {"at": time.time(), "error": None, "services": got["services"],
                         "events": summarize_events(got["events"])}
        except Exception as exc:  # PowerShell blocked or slow: the rest of the page still works
            self.slow = {**self.slow, "at": time.time(), "error": f"{type(exc).__name__}: {exc}"}
            log.warning("host health: services and event log not read: %s", self.slow["error"])

    def snapshot(self) -> dict:
        if not self.fast:
            self.sample()
        fast = dict(self.fast)
        if self.demo:
            fast = _demo_fast(fast)
        recent = [p for t, p in self.cpu if t >= time.time() - 5 * 60]
        return {"host": socket.gethostname(), "os": f"{platform.system()} {platform.release()} ({platform.version()})",
                "windows": sys.platform == "win32" or self.demo, **fast,
                "cpu": {"now": self.cpu[-1][1] if self.cpu else None,
                        "avg_5m": round(sum(recent) / len(recent), 1) if recent else None,
                        "series": [{"at": t, "pct": p} for t, p in self.cpu]},
                "services": self.slow.get("services"), "events": self.slow.get("events"),
                "windows_read_at": self.slow.get("at"), "windows_error": self.slow.get("error")}


# --------------------------------------------------------------------------------------------- demo
def _demo_fast(fast: dict) -> dict:
    """A plausible HMI: an almost full C: drive, so the demo has a disk alert to show."""
    gb = 1024 ** 3
    return {**fast, "disks": [{"path": "C:", "total": 128 * gb, "free": int(9.6 * gb), "used_pct": 92.5, "data": True}],
            "memory": {"total": 16 * gb, "available": int(6.1 * gb), "used_pct": 61.9},
            "uptime_s": 9 * 3600 + 1200, "boot_at": time.time() - (9 * 3600 + 1200)}


def _demo_windows() -> dict:
    now = int(time.time())
    services = [
        {"name": "FactoryTalk Linx Gateway", "display": "FactoryTalk Linx Gateway", "state": "Running", "start_mode": "Auto"},
        {"name": "RSLinx Enterprise", "display": "FactoryTalk Linx", "state": "Running", "start_mode": "Auto"},
        {"name": "FTActivationBoost", "display": "FactoryTalk Activation Helper", "state": "Running", "start_mode": "Auto"},
        {"name": "RsDirMux", "display": "Rockwell Directory Multiplexor", "state": "Running", "start_mode": "Auto"},
        {"name": "FTViewSEHistorian", "display": "FactoryTalk View SE Historian", "state": "Stopped", "start_mode": "Manual"},
        {"name": "FTDiagnostics", "display": "FactoryTalk Diagnostics Collector", "state": "Stopped", "start_mode": "Auto"},
    ]
    events = [{"log": "Application", "source": "FactoryTalk Diagnostics", "id": 3, "level": "error", "at": now - 600 * i,
               "message": "Unable to write to the diagnostics log: the log is full."} for i in range(14)]
    events += [{"log": "System", "source": "Microsoft-Windows-Kernel-Power", "id": 41, "level": "critical",
                "at": now - 9 * 3600, "message": "The system has rebooted without cleanly shutting down first."},
               {"log": "System", "source": "Service Control Manager", "id": 7031, "level": "error", "at": now - 9 * 3600,
                "message": "The FactoryTalk Diagnostics Collector service terminated unexpectedly."}]
    return {"services": services, "events": summarize_events(events)}

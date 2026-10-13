"""Alerts in the Windows Application event log, source "GhostMap", so the plant's existing tools (Event Viewer,
an event forwarder, a monitoring agent) see them without knowing anything about Ghost Map.

The source is registered with Windows' own EventCreate.exe as its message file (the same thing the
``eventcreate`` command does), whose messages 1 to 1000 are just the text given, so the events read cleanly in
Event Viewer. Registering needs administrator rights: ``service install`` does it, and so does the service
itself (LocalSystem) before its first event. Without the rights, and on other systems, writing is quietly skipped.
"""

from __future__ import annotations

import logging
import sys

SOURCE = "GhostMap"
KEY = r"SYSTEM\CurrentControlSet\Services\EventLog\Application\GhostMap"
MESSAGE_FILE = r"%SystemRoot%\System32\EventCreate.exe"
INFO, WARNING, ERROR = 0x0004, 0x0002, 0x0001  # EVENTLOG_*_TYPE
# Event IDs (1..1000 for EventCreate.exe's messages): one per area, +1 when the alert clears.
AREA_IDS = {"hmi": 100, "comms": 200, "machine": 300, "network": 400, "maintenance": 500, "production": 600}
log = logging.getLogger("ghostmap.winevent")
_state = {"registered": None}


def event_id(area: str, cleared: bool = False) -> int:
    return AREA_IDS.get(area, 700) + (1 if cleared else 0)


def register() -> bool:  # pragma: no cover - Windows only
    """Create the event source if it isn't there yet. False without the rights to (not an error)."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY):
            return True
    except OSError:
        pass
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "EventMessageFile", 0, winreg.REG_EXPAND_SZ, MESSAGE_FILE)
            winreg.SetValueEx(k, "TypesSupported", 0, winreg.REG_DWORD, 7)
        return True
    except OSError as exc:
        log.info("Windows event log source not registered (%s); alerts go to Ghost Map's own log only", exc)
        return False


def write(kind: int, eid: int, text: str) -> bool:  # pragma: no cover - Windows only (Windows CI job)
    """One event in the Application log. Returns False when it can't be written (never raises)."""
    if sys.platform != "win32":
        return False
    if _state["registered"] is None:
        _state["registered"] = register()
    if not _state["registered"]:
        return False
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.RegisterEventSourceW.restype = wintypes.HANDLE
    advapi.RegisterEventSourceW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    advapi.ReportEventW.argtypes = [wintypes.HANDLE, wintypes.WORD, wintypes.WORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.WORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPCWSTR), wintypes.LPVOID]
    advapi.DeregisterEventSource.argtypes = [wintypes.HANDLE]
    h = advapi.RegisterEventSourceW(None, SOURCE)
    if not h:
        return False
    try:
        strings = (wintypes.LPCWSTR * 1)(text[:31000])
        return bool(advapi.ReportEventW(h, kind, 0, eid, None, 1, 0, strings, None))
    finally:
        advapi.DeregisterEventSource(h)

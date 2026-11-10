"""Windows services and event-log errors, read through the OS APIs directly (no child process).

This replaces the old ``powershell.exe`` call in ``host.py``. A Windows service that launched a child process every
few minutes to read this looked like malware to Defender's behaviour heuristics and cost a process spawn each time;
these calls run in-process. Everything here is read-only: it enumerates and queries, and never starts, stops or
changes a service or clears a log.

Only the ctypes glue is Windows-only (``pragma: no cover``); the parsing (``_event_from_xml``, ``_iso_to_unix`` and
the small state maps) is pure so it is tested on every platform. On Linux/Docker this module isn't used at all:
Windows services and the Windows event log have no equivalent there, so ``host.py`` returns nothing for them, exactly
as the PowerShell path did off Windows.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree as ET

log = logging.getLogger("ghostmap.host")

# dwCurrentState (winsvc.h) -> the same text Win32_Service.State used, so analysis/alerts.py is unchanged.
SERVICE_STATES = {1: "Stopped", 2: "Start Pending", 3: "Stop Pending", 4: "Running",
                  5: "Continue Pending", 6: "Pause Pending", 7: "Paused"}
# dwStartType (winnt.h) -> the same text Win32_Service.StartMode used.
START_TYPES = {0: "Boot", 1: "System", 2: "Auto", 3: "Manual", 4: "Disabled"}

_EVT_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}


def _iso_to_unix(text: Optional[str]) -> Optional[int]:
    """A Windows event's ``SystemTime`` (ISO 8601 UTC, e.g. ``2026-11-06T09:24:08.1234567Z``) as a Unix second."""
    if not text:
        return None
    s = text.strip().replace("Z", "+00:00")
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)  # trim .NET's 100-ns (7+ digit) fraction to microseconds
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=datetime.fromisoformat(s).tzinfo or timezone.utc).timestamp())
    except ValueError:
        return None


def _event_from_xml(xml_text: str, message: str = "") -> Optional[dict]:
    """One event from its rendered ``<Event>`` XML, in the shape ``host.py`` expects (``log``, ``source``, ``id``,
    ``level``, ``at``, ``message``). ``message`` is the formatted text, which the XML itself doesn't carry."""
    try:
        system = ET.fromstring(xml_text).find("e:System", _EVT_NS)
    except ET.ParseError:
        return None
    if system is None:
        return None

    def text_of(tag: str) -> str:
        el = system.find(f"e:{tag}", _EVT_NS)
        return (el.text or "").strip() if el is not None else ""

    provider = system.find("e:Provider", _EVT_NS)
    level = text_of("Level")
    tc = system.find("e:TimeCreated", _EVT_NS)
    return {
        "log": text_of("Channel"),
        "source": (provider.get("Name") if provider is not None else "") or "",
        "id": int(text_of("EventID") or 0),
        "level": "critical" if level == "1" else "error",
        "at": _iso_to_unix(tc.get("SystemTime") if tc is not None else None),
        "message": (message or "").splitlines()[0] if message else "",
    }


# --------------------------------------------------------------------------- Windows-only ctypes glue
def read_services(match: str) -> list[dict]:  # pragma: no cover - Windows only
    """Every service whose display name matches ``match``, with its state and start type, via the service control
    manager (``EnumServicesStatusEx`` + ``QueryServiceConfig``). Read-only handles only."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    SC_MANAGER_ENUMERATE_SERVICE, SERVICE_QUERY_CONFIG = 0x0004, 0x0001
    SERVICE_WIN32, SERVICE_STATE_ALL, SC_ENUM_PROCESS_INFO = 0x30, 0x03, 0
    ERROR_MORE_DATA = 234

    class SERVICE_STATUS_PROCESS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD), ("dwProcessId", wintypes.DWORD), ("dwServiceFlags", wintypes.DWORD)]

    class ENUM_SERVICE_STATUS_PROCESSW(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpDisplayName", wintypes.LPWSTR),
                    ("ServiceStatusProcess", SERVICE_STATUS_PROCESS)]

    class QUERY_SERVICE_CONFIGW(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwStartType", wintypes.DWORD),
                    ("dwErrorControl", wintypes.DWORD), ("lpBinaryPathName", wintypes.LPWSTR),
                    ("lpLoadOrderGroup", wintypes.LPWSTR), ("dwTagId", wintypes.DWORD),
                    ("lpDependencies", wintypes.LPWSTR), ("lpServiceStartName", wintypes.LPWSTR),
                    ("lpDisplayName", wintypes.LPWSTR)]

    pattern = re.compile(match)
    scm = advapi.OpenSCManagerW(None, None, SC_MANAGER_ENUMERATE_SERVICE)
    if not scm:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed, returned, resume = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        advapi.EnumServicesStatusExW(scm, SC_ENUM_PROCESS_INFO, SERVICE_WIN32, SERVICE_STATE_ALL,
                                     None, 0, ctypes.byref(needed), ctypes.byref(returned),
                                     ctypes.byref(resume), None)
        if ctypes.get_last_error() != ERROR_MORE_DATA:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = (ctypes.c_byte * needed.value)()
        if not advapi.EnumServicesStatusExW(scm, SC_ENUM_PROCESS_INFO, SERVICE_WIN32, SERVICE_STATE_ALL,
                                            buf, needed.value, ctypes.byref(needed), ctypes.byref(returned),
                                            ctypes.byref(resume), None):
            raise ctypes.WinError(ctypes.get_last_error())
        arr = ctypes.cast(buf, ctypes.POINTER(ENUM_SERVICE_STATUS_PROCESSW * returned.value)).contents
        out = []
        for svc in arr:
            display = svc.lpDisplayName or ""
            if not pattern.search(display):
                continue
            state = SERVICE_STATES.get(svc.ServiceStatusProcess.dwCurrentState)
            out.append({"name": svc.lpServiceName or "", "display": display, "state": state,
                        "start_mode": _start_mode(advapi, scm, svc.lpServiceName, SERVICE_QUERY_CONFIG,
                                                  QUERY_SERVICE_CONFIGW)})
        return sorted(out, key=lambda s: (s["display"] or "").lower())
    finally:
        advapi.CloseServiceHandle(scm)


def _start_mode(advapi, scm, name, access, config_type) -> Optional[str]:  # pragma: no cover - Windows only
    """A service's start type (Auto/Manual/Disabled), or None if it can't be read (don't fail the whole list)."""
    import ctypes
    from ctypes import wintypes

    h = advapi.OpenServiceW(scm, name, access)
    if not h:
        return None
    try:
        needed = wintypes.DWORD()
        advapi.QueryServiceConfigW(h, None, 0, ctypes.byref(needed))
        buf = (ctypes.c_byte * needed.value)()
        if not advapi.QueryServiceConfigW(h, buf, needed.value, ctypes.byref(needed)):
            return None
        cfg = ctypes.cast(buf, ctypes.POINTER(config_type)).contents
        return START_TYPES.get(cfg.dwStartType)
    finally:
        advapi.CloseServiceHandle(h)


def read_events(hours: int, levels=(1, 2), channels=("System", "Application"), cap: int = 1000) -> list[dict]:  # pragma: no cover - Windows only
    """Critical (level 1) and error (level 2) events from the last ``hours``, newest first, via the modern event-log
    API (``EvtQuery`` / ``EvtRender`` / ``EvtFormatMessage``). Read-only."""
    import ctypes
    from ctypes import wintypes

    wevt = ctypes.WinDLL("wevtapi", use_last_error=True)
    EvtQueryChannelPath, EvtQueryReverseDirection = 0x1, 0x200
    EvtRenderEventXml = 1
    EvtFormatMessageEvent = 1
    ERROR_INSUFFICIENT_BUFFER = 122

    level_test = " or ".join(f"Level={n}" for n in levels)
    ms = int(hours) * 3600 * 1000
    query = f"*[System[({level_test}) and TimeCreated[timediff(@SystemTime) <= {ms}]]]"

    def render_xml(event) -> str:
        size, used, props = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        wevt.EvtRender(None, event, EvtRenderEventXml, 0, None, ctypes.byref(used), ctypes.byref(props))
        if ctypes.get_last_error() not in (ERROR_INSUFFICIENT_BUFFER, 0):
            return ""
        buf = ctypes.create_string_buffer(used.value)
        size = wintypes.DWORD(used.value)
        if not wevt.EvtRender(None, event, EvtRenderEventXml, size, buf, ctypes.byref(used), ctypes.byref(props)):
            return ""
        return buf.raw[:used.value].decode("utf-16-le", "replace").rstrip("\x00")

    def format_message(event) -> str:
        system = None
        try:
            system = ET.fromstring(render_xml(event)).find("e:System/e:Provider", _EVT_NS)
        except ET.ParseError:
            return ""
        provider = system.get("Name") if system is not None else None
        if not provider:
            return ""
        meta = wevt.EvtOpenPublisherMetadata(None, provider, None, 0, 0)
        if not meta:
            return ""
        try:
            used = wintypes.DWORD()
            wevt.EvtFormatMessage(meta, event, 0, 0, None, EvtFormatMessageEvent, 0, None, ctypes.byref(used))
            if not used.value:
                return ""
            buf = (ctypes.c_wchar * used.value)()
            if not wevt.EvtFormatMessage(meta, event, 0, 0, None, EvtFormatMessageEvent, used.value, buf,
                                         ctypes.byref(used)):
                return ""
            return buf.value
        finally:
            wevt.EvtClose(meta)

    events: list[dict] = []
    for channel in channels:
        h = wevt.EvtQuery(None, channel, query, EvtQueryChannelPath | EvtQueryReverseDirection)
        if not h:
            log.debug("event log: can't query %s: %s", channel, ctypes.WinError(ctypes.get_last_error()))
            continue
        try:
            got = (wintypes.HANDLE * 20)()
            returned = wintypes.DWORD()
            while len(events) < cap and wevt.EvtNext(h, 20, got, 0xFFFFFFFF, 0, ctypes.byref(returned)):
                for i in range(returned.value):
                    ev = _event_from_xml(render_xml(got[i]), format_message(got[i]))
                    if ev:
                        events.append(ev)
                    wevt.EvtClose(got[i])
        finally:
            wevt.EvtClose(h)
    return events


def read_windows(match: str, hours: int) -> dict:  # pragma: no cover - Windows only
    """Services and events together, in the shape ``host.py`` used from the PowerShell JSON."""
    return {"services": read_services(match), "events": read_events(hours)}

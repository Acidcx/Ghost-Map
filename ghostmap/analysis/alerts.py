"""One list of what needs someone's attention, from every part of Ghost Map.

Pure functions: each takes what one part of Ghost Map already knows (the HMI's health, a dashboard's comms
health and open stop, the switch monitor's open events, the passive listeners' events, service items, the TSC
link) and returns alerts. ``web/alerts.py`` gathers them, keeps track of when each started and reports new and
cleared ones to the log and the Windows event log.

An alert: ``key`` (stable while the condition lasts), dotted ``code``, ``severity`` ("error" or "warning"),
``area`` (hmi, comms, machine, network, maintenance, production), ``title``, ``detail``, ``hint`` (what to do)
and ``link`` (the page that shows more: ``{"tab": ..., "dash": ..., "view": ...}``).
"""

from __future__ import annotations

from typing import Optional

from ghostmap.web.auth import is_ixon

ERROR, WARNING = "error", "warning"
GB = 1024 ** 3

DISK_WARN_PCT, DISK_ERR_PCT = 90.0, 97.0
DISK_WARN_FREE, DISK_ERR_FREE = 2 * GB, GB // 2
MEM_WARN_PCT, MEM_ERR_PCT = 90.0, 95.0
CPU_WARN_PCT = 90.0
EVENT_SOURCE_WARN = 10            # errors from one source in the window
STOP_LONG_S = 10 * 60             # a stop this long is worth telling someone about
COMMS_DOWN_S = 60                 # no good read for this long
SWITCH_UNREACHABLE_POLLS = 3      # failed polls in a row (by time) before a switch counts as unreachable
RECENT_NETWORK_S = 24 * 3600      # how long a passive event (new device, IP conflict) stays on the list
RECENT_REFUSED_S = 3600           # how long a refused web client stays on the list
NETWORK_CODES = {"passive.arp_unknown": WARNING, "passive.ip_conflict": ERROR, "passive.bootp_known": ERROR,
                 "passive.bootp": WARNING, "passive.restart": WARNING}
# The services the machine can't run without, stopped is an error; the rest a warning. Linx is how Ghost Map and the
# HMI reach the PLCs, the Tag and Alarm servers feed the HMI screens, SQL Server and TSC hold the part schedule.
CRITICAL_SERVICES = ("linx", "tag server", "alarm", "sql server (", "tsc", "bradbury")
NORMALLY_RUNNING_S = 24 * 3600    # a service seen running this recently is expected to run, whatever its start mode


def alert(key: str, code: str, severity: str, area: str, title: str, detail: str, hint: str,
          link: Optional[dict] = None, at: Optional[float] = None) -> dict:
    return {"key": key, "code": code, "severity": severity, "area": area, "title": title, "detail": detail,
            "hint": hint, "link": link or {}, "at": at}


def _gb(n) -> str:
    return f"{n / GB:.1f} GB"


def _pct(v: float) -> str:
    return f"{int(v + 0.5)}%"  # rounded half up, like the page shows it


# ------------------------------------------------------------------------------------------------ this PC
def _expected_running(s: dict, now: Optional[float]) -> bool:
    mode = (s.get("start_mode") or "").lower()
    if mode == "auto":
        return True
    last = s.get("last_running")
    return mode != "disabled" and last is not None and now is not None and now - last <= NORMALLY_RUNNING_S


def host_alerts(snap: dict, now: Optional[float] = None) -> list[dict]:
    now = snap.get("windows_read_at") if now is None else now
    out = []
    link = {"tab": "hostpc"}
    for d in snap.get("disks") or []:
        pct, free = d.get("used_pct"), d.get("free")
        if pct is None or free is None:
            continue
        sev = ERROR if pct >= DISK_ERR_PCT or free < DISK_ERR_FREE else WARNING if pct >= DISK_WARN_PCT or free < DISK_WARN_FREE else None
        if sev:
            out.append(alert(f"hmi.disk_low:{d['path']}", "hmi.disk_low", sev, "hmi", f"Disk {d['path']} is {_pct(pct)} full",
                             f"{_gb(free)} free of {_gb(d['total'])}" + (" (Ghost Map's data is on it)" if d.get("data") else ""),
                             "Free space before Windows, FactoryTalk or the history database run out: old FactoryTalk "
                             "Diagnostics logs, View datalogs and alarm logs, backups and installers in Downloads, "
                             "and Disk Cleanup > Clean up system files.", link))
    m = snap.get("memory") or {}
    if m.get("used_pct") is not None and m["used_pct"] >= MEM_WARN_PCT:
        out.append(alert("hmi.memory_high", "hmi.memory_high", ERROR if m["used_pct"] >= MEM_ERR_PCT else WARNING, "hmi",
                         f"Memory {_pct(m['used_pct'])} used", f"{_gb(m['available'])} free of {_gb(m['total'])}",
                         "Open Task Manager > Details and sort by Memory. A FactoryTalk View client or a browser that "
                         "keeps growing is the usual cause; restart it at a planned stop.", link))
    cpu = (snap.get("cpu") or {}).get("avg_5m")
    if cpu is not None and cpu >= CPU_WARN_PCT:
        out.append(alert("hmi.cpu_high", "hmi.cpu_high", WARNING, "hmi", f"CPU {_pct(cpu)} busy for 5 minutes",
                         "The HMI may respond slowly and screens update late.",
                         "Open Task Manager > Details and sort by CPU. Antivirus scans and Windows Update are the usual "
                         "suspects; schedule them outside production.", link))
    for s in snap.get("services") or []:
        if (s.get("state") or "").lower() != "running" and _expected_running(s, now):
            name = s.get("display") or s.get("name")
            low = name.lower()
            critical = any(w in low for w in CRITICAL_SERVICES)
            why = ("Set to start automatically, but not running." if (s.get("start_mode") or "").lower() == "auto"
                   else "Set to start manually, but it was running until recently, so something stopped it.")
            if "linx" in low:
                why += " Without it the HMI and Ghost Map can't reach the PLCs."
            elif critical:
                why += " The machine's screens or production data depend on it."
            if (s.get("stops") or 0) > 1:
                why += f" It has stopped {s['stops']} times in the last 7 days."
            out.append(alert(f"hmi.service_stopped:{s.get('name')}", "hmi.service_stopped", ERROR if critical else WARNING,
                             "hmi", f"{name} is {(s.get('state') or 'not running').lower()}", why,
                             "Start it in services.msc (Ghost Map only looks; it never starts or stops services). If it "
                             "stops again, the Application event log says why.", link))
    ev = snap.get("events") or {}
    for g in ev.get("top") or []:
        if g["level"] == "critical":
            out.append(alert(f"hmi.event_critical:{g['source']}:{g['id']}", "hmi.event_critical", WARNING, "hmi",
                             f"Critical Windows event: {g['source']} {g['id']}",
                             f"{g['count']}x in the last {ev.get('window_h', 24)} h. {g['message']}".strip(),
                             "Event Viewer > Windows Logs > " + (g.get("log") or "System") + ", filter by this source. "
                             "Kernel-Power 41 means the PC lost power or was switched off without shutting down.", link,
                             g.get("last")))
        elif g["count"] >= EVENT_SOURCE_WARN:
            out.append(alert(f"hmi.event_errors:{g['source']}:{g['id']}", "hmi.event_errors", WARNING, "hmi",
                             f"{g['count']} errors from {g['source']} in {ev.get('window_h', 24)} h",
                             f"Event {g['id']}: {g['message']}".strip(),
                             "Event Viewer > Windows Logs > " + (g.get("log") or "Application") + ", filter by this "
                             "source. A burst of the same error usually points at one misbehaving program or service.",
                             link, g.get("last")))
    return out


# ------------------------------------------------------------------------------------------- dashboards
def comms_alerts(dash: dict, health: dict, collector: dict, now: float) -> list[dict]:
    """A dashboard whose data can't be trusted right now."""
    did, name = dash["id"], dash.get("name") or dash["id"]
    link = {"tab": "machine", "dash": did, "view": "health"}
    last_ok = collector.get("last_ok") or health.get("last_ok")
    error = collector.get("error") or health.get("last_error")
    if error and (last_ok is None or now - last_ok > COMMS_DOWN_S):
        return [alert(f"comms.down:{did}", "comms.down", ERROR, "comms", f"{name}: no data from the gateway",
                      str(error), "Check the gateway PC is on and FactoryTalk Linx Gateway is running, and that this PC "
                      "can reach it. Until then alarms show grey and nothing is recorded.", link)]
    out = []
    hb = health.get("heartbeat") or {}
    if hb.get("frozen"):
        out.append(alert(f"comms.frozen:{did}", "comms.frozen", ERROR, "comms", f"{name}: data may be frozen",
                         f"The heartbeat tag hasn't changed for {round(hb.get('age_s') or 0)} s although reads succeed.",
                         "The gateway is serving old values: check the PLC is in Run and the Linx path to it in "
                         "FactoryTalk Linx. Faults show \"?\" until the heartbeat moves.", link))
    total, good = health.get("total") or 0, health.get("good")
    if total and good is not None and good < total:
        out.append(alert(f"comms.tags_bad:{did}", "comms.tags_bad", WARNING, "comms",
                         f"{name}: {total - good} of {total} tags not readable",
                         ", ".join(f"{k} {v}" for k, v in sorted((health.get("bad_by_plc") or {}).items())) or "",
                         "Renamed or deleted tags in the PLC, or a controller that's offline. The Health page lists them; "
                         "Check all alarms finds the ones to fix in the layout.", link))
    cert = health.get("cert_changed")
    if cert:
        out.append(alert(f"comms.certificate_changed:{did}", "comms.certificate_changed", WARNING, "comms",
                         f"{name}: the gateway's certificate changed", f"New SHA-256 {cert.get('sha256', '')[:16]}...",
                         "If the gateway was reinstalled or its certificate renewed, accept it in Tags (OPC UA) > Gateway "
                         "certificates. If not, find out what is answering at the gateway's address.", {"tab": "opcua"}))
    return out


def stop_alerts(dash: dict, stop: Optional[dict], now: float) -> list[dict]:
    """A machine that has been stopped for a while, with what stopped it."""
    if not stop or stop.get("end") is not None or now - stop["start"] < STOP_LONG_S:
        return []
    did, name = dash["id"], dash.get("name") or dash["id"]
    firsts = ", ".join(f"{f['label']} ({f['area_title']})" if f.get("area_title") else f["label"]
                       for f in stop.get("first_out") or [])
    mins = int((now - stop["start"]) // 60)
    return [alert(f"machine.stopped:{did}", "machine.stopped", WARNING, "machine", f"{name} stopped for {mins} min",
                  f"First out: {firsts}" if firsts else "First out unknown (the stop began while the data was unreadable).",
                  "The first-out alarm is what to look at first. The History page shows how often this stop happens.",
                  {"tab": "machine", "dash": did, "view": "history"}, stop["start"])]


# ------------------------------------------------------------------------------------------------- network
def switch_alerts(open_events: list[dict], status: dict, now: float) -> list[dict]:
    """Open events from the switch traffic monitor, and switches it can't read."""
    out = []
    for e in open_events:
        if e.get("passing_from"):
            continue  # the same storm seen on an uplink: shown once, at the port it comes from
        if e.get("severity") not in (ERROR, WARNING):
            continue
        where = f"{e.get('name') or e['switch']}{' ' + e['port'] if e.get('port') else ''}"
        peak = f"Peak {e['peak']:,.0f} {e.get('unit') or ''}".strip() if e.get("peak") is not None else ""
        out.append(alert(f"{e['code']}:{e['switch']}:{e.get('port') or ''}", e["code"], e["severity"], "network",
                         f"{where}: {e['message']}", peak, e.get("hint") or "", {"tab": "traffic"}, e["start"]))
    interval = status.get("interval_s") or 30
    for sw in status.get("switches") or []:
        if sw.get("error") and (sw.get("last_ok") is None or now - sw["last_ok"] > SWITCH_UNREACHABLE_POLLS * interval):
            out.append(alert(f"switch.unreachable:{sw['switch']}", "switch.unreachable", WARNING, "network",
                             f"Switch {sw.get('name') or sw['switch']} not answering SNMP", str(sw["error"]),
                             "Check the switch is powered and reachable, and that the SNMP community or user in "
                             "Network > Traffic still matches the switch's configuration.", {"tab": "traffic"},
                             sw.get("error_at")))
    return out


def network_alerts(events: list[dict], now: float) -> list[dict]:
    """New devices, IP conflicts and restarts heard by the passive listeners in the last day (the latest of each)."""
    out: dict[str, dict] = {}
    for e in events:
        sev = NETWORK_CODES.get(e.get("code"))
        if sev is None or now - e.get("at", 0) > RECENT_NETWORK_S:
            continue
        who = e.get("mac") or e.get("ip") or e.get("source") or ""
        key = f"{e['code']}:{who}"
        if key in out and out[key]["at"] >= e["at"]:
            continue
        out[key] = alert(key, e["code"].replace("passive.", "network."), sev, "network", e["text"], "",
                         e.get("hint") or "Network > Live shows what was heard and when.", {"tab": "live"}, e["at"])
    return list(out.values())


# -------------------------------------------------------------------------------------------- web access
def access_alerts(refused: list[dict], now: float) -> list[dict]:
    """Addresses the ``--allow`` list turned away in the last hour: usually the IXrouter or IXON's VPN arriving
    from an address that wasn't expected, sometimes someone else on the network trying the UI."""
    out = []
    for r in refused:
        if now - r["last"] > RECENT_REFUSED_S:
            continue
        times = "once" if r["count"] == 1 else f"{r['count']} times"
        if is_ixon(r["ip"]):
            hint = (f"{r['ip']} is in IXON's VPN range, which changes when the IXrouter restarts. Allow the whole range "
                    "(and its firewall rule): GhostMap.exe service install --ixon, from an elevated prompt, plus "
                    "--https if it is in use.")
        else:
            hint = (f"If {r['ip']} should reach Ghost Map, add it: GhostMap.exe service install with --allow {r['ip']} "
                    "(plus the --host, --allow and --ixon already in use); install updates the firewall rule too. "
                    "If not, find out who is trying to open Ghost Map.")
        out.append(alert(f"web.refused:{r['ip']}", "web.refused", WARNING, "hmi",
                         f"Refused web access from {r['ip']}",
                         f"Turned away {times}, last for {r.get('path') or '/'}. Only localhost and --allow may connect.",
                         hint,
                         {"tab": "hostpc"}, r["first"]))
    return out


# --------------------------------------------------------------------------------------- maintenance / TSC
def maintenance_alerts(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if it.get("state") != "overdue":
            continue
        over = (it.get("value") or 0) - it["interval"]
        out.append(alert(f"maintenance.overdue:{it['id']}", "maintenance.overdue", WARNING, "maintenance",
                         f"Service overdue: {it['name']}",
                         f"{over:,.0f} {it.get('unit', '')} past its {it['interval']:,.0f} {it.get('unit', '')} interval".strip(),
                         "Do the service and press Mark done on Maintenance > Service, so the count starts again.",
                         {"tab": "service"}))
    return out


def tsc_alerts(status: dict, now: float) -> list[dict]:
    if not status.get("configured") or not status.get("last_error"):
        return []
    if status.get("last_ok") and status["last_ok"] > (status.get("last_error_at") or 0):
        return []
    return [alert("production.tsc_unreachable", "production.tsc_unreachable", WARNING, "production",
                  "TSC part schedule not answering", str(status["last_error"]),
                  "Check the SQL Server PC is on and reachable, and that the read-only login still works "
                  "(Production > Connection > Test connection).", {"tab": "tscconn"}, status.get("last_error_at"))]

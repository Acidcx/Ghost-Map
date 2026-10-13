"""Turn what the passive listeners heard into events an engineer can act on (pure functions, no I/O).

Each event: {kind, level: err | warn | info | ok, code, text, hint, ip, mac, source}. ``known`` maps MAC -> a
label from the newest scan (e.g. "PowerFlex 755 (192.168.1.20)") so events can name the device.
"""

from __future__ import annotations

import re
from typing import Optional

from ghostmap.protocols.oui import mac_vendor

IF_INDEX = "1.3.6.1.2.1.2.2.1.1."
IF_DESCR = "1.3.6.1.2.1.2.2.1.2."
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1."
IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18."


def _ev(kind, level, code, text, hint="", ip="", mac="", source="") -> dict:
    return {"kind": kind, "level": level, "code": code, "text": text, "hint": hint, "ip": ip, "mac": mac, "source": source}


def _who(mac: str, known: dict) -> str:
    if mac in known:
        return known[mac]
    v = mac_vendor(mac)
    return f"{mac} ({v})" if v else mac


def arp_changes(prev: dict, now: dict, known: dict) -> list[dict]:
    """New addresses and addresses that changed MAC, between two reads of this PC's ARP cache (``{ip: mac}``).
    The first read (``prev`` empty) is the baseline and gives no events. Entries that age out are not reported:
    the OS drops them after a few minutes of silence, so it says nothing about the device."""
    if not prev:
        return []
    out = []
    for ip, mac in sorted(now.items()):
        old = prev.get(ip)
        if old is None:
            if mac in known:
                out.append(_ev("arp", "info", "passive.arp_new", f"{known[mac]} is talking on the network.", ip=ip, mac=mac))
            else:
                out.append(_ev("arp", "warn", "passive.arp_unknown", f"{ip} ({_who(mac, known)}) appeared and was not in the last scan.",
                               "If nobody plugged a device in, find it by its MAC in the switch's MAC table (Switches page after a scan).",
                               ip=ip, mac=mac))
        elif old != mac:
            out.append(_ev("arp", "err", "passive.ip_conflict", f"{ip} now answers from {_who(mac, known)}; it was {_who(old, known)}.",
                           "Either the device was replaced, or two devices are set to the same IP (an IP conflict). "
                           "If both MACs keep coming back, find the second one and change its IP.", ip=ip, mac=mac))
    return out


def bootp_event(msg: dict, known: dict, known_ip: Optional[dict] = None) -> Optional[dict]:
    """A device asking for an IP address. Ghost Map never answers it."""
    if not msg.get("request") or not msg.get("mac"):
        return None
    mac = msg["mac"]
    proto = f"DHCP {msg['dhcp_type']}" if msg.get("dhcp_type") else "BOOTP"
    extra = ", ".join(x for x in (msg.get("hostname"), msg.get("vendor_class")) if x)
    if mac in known:
        return _ev("bootp", "err", "passive.bootp_known", f"{known[mac]} is asking for an IP address ({proto}).",
                   "This device had an IP in the last scan: it was reset or replaced, or BOOTP/DHCP was left on and it lost "
                   "its address after a power cycle. Set its IP again and turn BOOTP/DHCP off on it.",
                   ip=(known_ip or {}).get(mac, ""), mac=mac)
    return _ev("bootp", "warn", "passive.bootp", f"{_who(mac, known)} is asking for an IP address ({proto}){f': {extra}' if extra else ''}.",
               "A new or factory-reset module with no IP. Give it one with Rockwell's BOOTP/DHCP tool or its rotary "
               "switches, then turn BOOTP/DHCP off on it so it keeps the address.", mac=mac)


_IFACE = re.compile(r"Interface ([\w/.\-]+), changed state to ([\w ]+?)\.?$")
SYSLOG_RULES = {
    "PM-4-ERR_DISABLE": ("err", "passive.err_disable", "The switch shut a port down (err-disabled): {msg}",
                         "Fix the cause it names (loop, BPDU guard, port security, link flap), then bounce the port."),
    "SW_MATM-4-MACFLAP_NOTIF": ("err", "passive.mac_flap", "A MAC address keeps moving between ports: {msg}",
                                "Almost always a network loop (a cable plugged back into the same switch or ring). Find and remove it."),
    "CDP-4-DUPLEX_MISMATCH": ("warn", "passive.duplex", "Duplex mismatch: {msg}", "Set both ends to auto, or both to the same fixed setting."),
    "PORT_SECURITY-2-PSECURE_VIOLATION": ("err", "passive.port_security", "Port security blocked a device: {msg}",
                                          "Someone plugged a different device into a locked port, or a replaced device has a new MAC."),
    "SPANTREE-2-ROOTGUARD_BLOCK": ("err", "passive.stp_block", "Spanning tree blocked a port: {msg}", "Another switch tried to become root."),
    "SPANTREE-5-ROOTCHANGE": ("warn", "passive.stp_root", "Spanning tree root changed: {msg}", "A switch was added, removed or restarted."),
    "SPANTREE-5-TOPOTRAPS": ("warn", "passive.stp_change", "Spanning tree topology changed: {msg}", "A link went up or down somewhere in the network."),
    "SYS-5-RESTART": ("info", "passive.switch_restart", "The switch restarted: {msg}", ""),
    "SYS-5-CONFIG_I": ("info", "passive.switch_config", "The switch configuration was changed: {msg}", ""),
}


def syslog_event(msg: dict, source: str) -> dict:
    m, text = msg.get("mnemonic", ""), msg.get("message", "")
    i = _IFACE.search(text)
    if m.startswith("LINK-") and m.endswith("UPDOWN") and i:
        port, st = i.group(1), i.group(2).strip()
        down = "down" in st
        return _ev("syslog", "warn" if down else "ok", "passive.link_down" if down else "passive.link_up",
                   f"{source}: port {port} went {'down' if down else 'up'}.",
                   "Check the cable and the device on that port; repeated down/up is a loose connector or a failing port." if down else "",
                   source=source)
    if m.startswith("LINEPROTO-") and i:
        return _ev("syslog", "info", "passive.lineproto", f"{source}: line protocol on {i.group(1)} is {i.group(2).strip()}.", source=source)
    if m in SYSLOG_RULES:
        level, code, txt, hint = SYSLOG_RULES[m]
        return _ev("syslog", level, code, f"{source}: " + txt.format(msg=text), hint, source=source)
    sev = msg.get("severity_n", 6)
    level = "err" if sev <= 3 else "warn" if sev == 4 else "info"
    return _ev("syslog", level, "passive.syslog", f"{source}: {f'%{m} ' if m else ''}{text}", source=source)


def _port_of(varbinds: list) -> str:
    vals = {o.rsplit(".", 1)[0] + ".": v for o, v in varbinds}
    name = vals.get(IF_NAME) or vals.get(IF_DESCR) or ""
    alias = vals.get(IF_ALIAS) or ""
    if not name and IF_INDEX in vals:
        name = f"ifIndex {vals[IF_INDEX]}"
    return f"{name}{f' ({alias})' if alias else ''}"


def trap_event(trap: dict, source: str) -> dict:
    t = trap.get("trap", "")
    if trap.get("version") == "v3":
        return _ev("trap", "info", "passive.trap_v3", f"{source} sent an SNMPv3 trap (encrypted, not decoded).", source=source)
    if t in ("linkDown", "linkUp"):
        port = _port_of(trap["varbinds"]) or "a port"
        down = t == "linkDown"
        return _ev("trap", "warn" if down else "ok", "passive.link_down" if down else "passive.link_up",
                   f"{source}: {port} went {'down' if down else 'up'}.",
                   "Check the cable and the device on that port." if down else "", source=source)
    if t in ("coldStart", "warmStart"):
        return _ev("trap", "warn", "passive.restart", f"{source} restarted ({'power cycle' if t == 'coldStart' else 'software restart'}).",
                   "If nobody restarted it, check its power supply and its log.", source=source)
    if t == "authenticationFailure":
        return _ev("trap", "warn", "passive.snmp_auth", f"{source}: something used a wrong SNMP community on it.",
                   "Another tool with old settings, or someone probing the network.", source=source)
    vb = ", ".join(f"{o}={v}" for o, v in trap.get("varbinds", [])[:3])
    return _ev("trap", "info", "passive.trap", f"{source} sent trap {t or '?'}{f' ({vb})' if vb else ''}.", source=source)

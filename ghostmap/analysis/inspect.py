"""Device inspector: what the saved scans know about one device, and a verdict on what the inspector found
(pure functions, no I/O)."""

from __future__ import annotations

from typing import Optional

from ghostmap.models import ScanResult, to_dict

JITTER_WARN_MS = 5.0
LATENCY_WARN_MS = 10.0
HISTORY_SHOWN = 10


def _port_info(scan: ScanResult, dev) -> tuple[Optional[dict], Optional[dict]]:
    sw = next((s for s in scan.switches if s.ip == dev.switch_ip), None)
    if sw is None or not dev.switch_port:
        return None, None
    p = next((p for p in sw.ports if p.name == dev.switch_port), None)
    if p is None:
        return {"name": sw.sys_name, "ip": sw.ip}, None
    devices_on_port = sum(1 for d in scan.devices if d.switch_ip == sw.ip and d.switch_port == p.name)
    return ({"name": sw.sys_name, "ip": sw.ip, "model": sw.model},
            {"name": p.name, "alias": p.alias, "oper_status": p.oper_status, "speed_mbps": p.speed_mbps, "duplex": p.duplex,
             "vlan": p.vlan, "in_errors": p.in_errors, "out_errors": p.out_errors, "fcs_errors": p.fcs_errors,
             "late_collisions": p.late_collisions, "in_discards": p.in_discards, "out_discards": p.out_discards,
             "is_uplink": p.is_uplink, "devices": devices_on_port})


def context(ip: str, scans: list[ScanResult]) -> dict:
    """The newest scan that saw ``ip``: the device, where it is plugged in, its findings; and what every saved
    scan saw at that address (to spot a swapped module or a firmware change). ``scans`` newest first."""
    out: dict = {"scan": None, "device": None, "switch": None, "port": None, "findings": [], "history": []}
    for scan in scans:
        dev = next((d for d in scan.devices if d.ip == ip), None)
        if dev is None:
            continue
        i = dev.identity
        out["history"].append({"scan": scan.id, "started": scan.started, "mac": dev.mac or "",
                               "product": i.product_name if i else "", "revision": i.revision if i else "",
                               "serial": i.serial_hex if i else "", "port": dev.switch_port or ""})
        if out["device"] is None:
            sw, port = _port_info(scan, dev)
            names = {dev.label, ip}
            out.update(scan={"id": scan.id, "started": scan.started}, device=to_dict(dev), switch=sw, port=port,
                       findings=[to_dict(f) for f in scan.findings
                                 if f.target in names or (port and sw and port["name"] in f.target and (sw["name"] or sw["ip"]) in f.target)])
    out["history"] = out["history"][:HISTORY_SHOWN]
    return out


def verdict(r: dict, ctx: dict) -> list[dict]:
    """Plain-language checks on an inspection: [{level: ok | warn | err | info, code, text, hint}], worst first."""
    out = []

    def add(level, code, text, hint=""):
        out.append({"level": level, "code": code, "text": text, "hint": hint})

    lat, ident, port = r["latency"], r.get("identity"), ctx.get("port")
    if lat["port"] is None:
        if ident:
            add("warn", "inspect.no_tcp", "Answers ListIdentity but no TCP port in the list is open.",
                "A firewall may allow UDP 44818 only; Studio 5000 and FT Linx need TCP 44818.")
        else:
            add("err", "inspect.unreachable", "No answer at all from this address.",
                "Check it is powered and on this subnet, the cable and the switch port, and that this PC has a route to it.")
    else:
        loss = lat["lost"] / lat["sent"] if lat["sent"] else 0
        if loss:
            add("err" if loss >= 0.2 else "warn", "inspect.loss", f"{lat['lost']} of {lat['sent']} connection attempts got no answer.",
                "Lost connects on a local network usually mean a bad cable, a duplex mismatch or an overloaded device.")
        if lat["avg"] is not None and lat["avg"] > LATENCY_WARN_MS:
            add("warn", "inspect.slow", f"Slow to answer: {lat['avg']} ms on average.",
                "On a machine network a device should answer in a few ms. Check the path for a busy uplink or a hub.")
        if lat["jitter"] is not None and lat["jitter"] > JITTER_WARN_MS:
            add("warn", "inspect.jitter", f"Response time jumps around by {lat['jitter']} ms between attempts.",
                "Jitter hurts I/O connections (RPI). Look at the switch port's errors and duplex.")
        if not loss and (lat["avg"] or 0) <= LATENCY_WARN_MS and (lat["jitter"] or 0) <= JITTER_WARN_MS:
            add("ok", "inspect.reachable", f"Answers reliably in {lat['avg']} ms (TCP {lat['port']}).")
    if ident:
        sock = ident.get("socket_ip")
        if sock and sock != r["ip"] and sock != "0.0.0.0":
            add("warn", "inspect.socket_ip", f"Says its own IP is {sock}, not {r['ip']}.",
                "NAT, a second interface, or the module's IP setting differs from what the network uses.")
        st = ident.get("state")
        if st in (4, 5):
            add("err", "inspect.major_fault", f"Reports a major fault ({ident.get('state_name')}).",
                "Check the module status LED and its fault log (web page or Studio 5000).")
        flags = ident.get("status_flags") or []
        if any("minor" in f.lower() for f in flags):
            add("warn", "inspect.minor_fault", "Reports a minor fault.", "Usually a recoverable fault; see its diagnostics page.")
        old = next((h for h in ctx.get("history", [])[1:] if h["serial"] and h["serial"] != ident.get("serial_hex")), None)
        if old:
            add("info", "inspect.swapped", f"Serial changed since scan {old['scan']} (was {old['serial']}): the module was replaced.")
        rev = next((h for h in ctx.get("history", [])[1:] if h["revision"] and h["revision"] != ident.get("revision")), None)
        if rev and not old:
            add("info", "inspect.firmware", f"Firmware changed since scan {rev['scan']}: {rev['revision']} → {ident.get('revision')}.")
    elif any(p["open"] and p["port"] == 44818 for p in r["ports"]):
        add("warn", "inspect.no_identity", "TCP 44818 is open but ListIdentity got no answer.", "UDP 44818 may be blocked on the path.")
    for p in r["ports"]:
        if p["open"] and p["note"]:
            add("info" if p["port"] == 80 else "warn", f"inspect.port_{p['port']}", f"{p['service']} is open (TCP {p['port']}).", p["note"])
    if port:
        if port["duplex"] == "half" and (port["speed_mbps"] or 0) >= 100:
            add("err", "inspect.half_duplex", f"Its switch port {port['name']} runs half duplex at {port['speed_mbps']} Mb.",
                "Almost always a duplex mismatch: set both ends to auto (or both fixed the same).")
        errs = sum(port.get(k) or 0 for k in ("in_errors", "fcs_errors", "late_collisions"))
        if errs:
            add("warn", "inspect.port_errors", f"Its switch port {port['name']} has counted {errs:,} errors.",
                "FCS errors point at the cable or connector; late collisions at duplex.")
        if port["devices"] > 1 and not port["is_uplink"]:
            add("info", "inspect.shared_port", f"{port['devices']} devices share switch port {port['name']}.",
                "An unmanaged switch or a DLR/linear ring hangs off this port.")
    snmp = r.get("snmp")
    if snmp and snmp.get("ok") and snmp.get("uptime_s") is not None and snmp["uptime_s"] < 86400:
        add("info", "inspect.rebooted", f"Restarted {round(snmp['uptime_s'] / 3600, 1)} h ago (SNMP uptime).")
    order = {"err": 0, "warn": 1, "info": 2, "ok": 3}
    return sorted(out, key=lambda c: order[c["level"]])

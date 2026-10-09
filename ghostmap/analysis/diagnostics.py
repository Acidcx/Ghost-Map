"""Rule based findings that point a commissioning engineer at likely problems."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ghostmap.models import Device, DiscoveredIdentity, Finding, Port, SwitchInfo
from ghostmap.protocols import cip_tables


@dataclass
class Thresholds:
    port_errors: int = 10  # FCS + alignment + input errors since counters were cleared
    late_collisions: int = 1
    macs_on_access_port: int = 1  # more than this on a non-uplink port is worth noting
    recent_reboot_seconds: int = 3600
    cpu_5min_percent: int = 80  # sustained switch CPU load
    cpu_5sec_percent: int = 95  # momentary spike, only noted
    memory_percent: float = 90.0
    stp_change_seconds: int = 3600  # a spanning tree change this recent is worth a look
    # Traffic (from two counter readings): link use, broadcast / multicast packets per second, drops
    util_percent: float = 70.0
    bcast_pps: float = 500.0  # received from one device: a storm or a loop starting
    bcast_storm_pps: float = 5000.0
    mcast_pps: float = 5000.0  # received from one device (EtherNet/IP multicast I/O is legitimately busy)
    mcast_flood_pps: float = 2000.0  # sent to an edge device: IGMP snooping off or no querier
    errors_per_s: float = 0.1  # input errors still climbing now
    drops_per_s: float = 1.0  # output drops: the port is congested


_STATE_TEXT = {"notFunctioning": "not working", "notPresent": "not present"}
_ENV_SEVERITY = {"warning": "warning", "critical": "error", "shutdown": "error", "notFunctioning": "warning"}


def _switch_health(sw: SwitchInfo, name: str, t: Thresholds) -> list[Finding]:
    h, out = sw.health, []
    if h.cpu_5m is not None and h.cpu_5m >= t.cpu_5min_percent:
        out.append(Finding("warning", "switch.cpu_high", name,
                           f"Switch CPU at {h.cpu_5m}% averaged over 5 minutes (1 min {h.cpu_1m}%, 5 s {h.cpu_5s}%).",
                           "A busy CPU answers SNMP, LLDP and spanning tree late. Usual causes: a broadcast or multicast "
                           "storm (IGMP snooping off), a loop, or many SNMP pollers. Check 'show processes cpu sorted'."))
    elif h.cpu_5s is not None and h.cpu_5s >= t.cpu_5sec_percent:
        out.append(Finding("info", "switch.cpu_spike", name, f"Switch CPU at {h.cpu_5s}% over the last 5 seconds.",
                           "Short spikes are normal during a scan or config save. Re-scan; if it stays high, see 'show processes cpu'."))
    mem = h.memory_percent
    if mem is not None and mem >= t.memory_percent:
        out.append(Finding("warning", "switch.memory_high", name, f"Switch memory {mem:.0f}% used.",
                           "Can lead to dropped management sessions or a crash. Check 'show memory statistics'; "
                           "a slow climb across scans points to a leak fixed in newer firmware."))
    for s in h.temperatures:
        sev = _ENV_SEVERITY.get(s.state)
        reading = f" ({s.celsius:.0f} °C)" if s.celsius is not None else ""
        if sev:
            out.append(Finding(sev, "switch.temperature", name, f"Temperature sensor '{s.name}' is {_STATE_TEXT.get(s.state, s.state)}{reading}.",
                               "Check the enclosure: blocked vents, failed panel fan or A/C, or the switch mounted next to a drive."))
        elif s.celsius is not None and s.threshold and s.celsius >= s.threshold:
            out.append(Finding("warning", "switch.temperature", name,
                               f"Temperature sensor '{s.name}' at {s.celsius:.0f} °C, at or above its {s.threshold:.0f} °C threshold.",
                               "Check the enclosure: blocked vents, failed panel fan or A/C, or the switch mounted next to a drive."))
    for kind, sensors, hint in (
        ("power", h.power_supplies, "One of the switch's power inputs is out. Fine if that input is deliberately "
                                    "unwired; otherwise check the 24 V supply, its breaker and the terminal block."),
        ("fan", h.fans, "Replace the fan or check what is blocking it."),
    ):
        for s in sensors:
            sev = _ENV_SEVERITY.get(s.state)
            if sev:
                out.append(Finding(sev, f"switch.{kind}", name, f"{kind.capitalize()} '{s.name}' is {_STATE_TEXT.get(s.state, s.state)}.", hint))
    for st in h.stp:
        if st.seconds_since_change is not None and st.seconds_since_change < t.stp_change_seconds and st.topology_changes:
            where = f"VLAN {st.vlan}" if st.vlan is not None else "spanning tree"
            rebooted = 0 < sw.uptime_seconds < t.recent_reboot_seconds  # the tree forms at boot; expected then
            out.append(Finding("info" if rebooted else "warning", "switch.stp_change", name,
                               f"{where}: topology changed {_ago(st.seconds_since_change)} ago "
                               f"({st.topology_changes} changes since the switch started).",
                               "Each change flushes MAC tables and can drop EtherNet/IP I/O connections. Look for a flapping link, "
                               "a device port without PortFast, or someone plugging in a switch. "
                               "'show spanning-tree detail' names the port that last changed."))
    return out


def _rate(v: float) -> str:
    return f"{v:,.0f}" if v >= 10 else f"{v:.1f}"


def _mbps(bps: float) -> str:
    return f"{bps / 1e6:.1f} Mbps" if bps < 1e8 else f"{bps / 1e6:,.0f} Mbps"


def traffic_findings(where: str, port: Port, t: Optional[Thresholds] = None) -> list[Finding]:
    """Findings from one port's rates (``port.traffic``). Also used by the background monitor."""
    t = t or Thresholds()
    tr, out = port.traffic, []
    if tr is None:
        return out
    window = f"over {tr.seconds:.0f} s"
    peer = port.link_to or "the next switch"
    chain = len(port.macs) > 1 and not port.is_uplink
    # Where packets "in" on this port come from: one device, a chain of them, or everything beyond a link.
    source = (f"arriving on the link from {peer}" if port.is_uplink
              else f"coming from the {len(port.macs)} devices on this port" if chain else "coming from this port")
    beyond = (f" The source is on the far side of this link: look at {peer}'s ports, or further along." if port.is_uplink
              else " Several devices share this port (daisy chain, ring or unmanaged switch): the source is one of them."
              if chain else "")
    for direction, util, bps in (("in", tr.in_util, tr.in_bps), ("out", tr.out_util, tr.out_bps)):
        if util is not None and util >= t.util_percent:
            what = "receiving from the device" if direction == "in" else "sending to the device"
            if chain:
                what = f"receiving from the {len(port.macs)} devices on this port" if direction == "in" else "sending to the devices on this port"
            if port.is_uplink:
                what = f"on the link from {peer}" if direction == "in" else f"on the link to {peer}"
            out.append(Finding("warning", "port.utilization", where,
                               f"Link {util:.0f}% busy {what} ({_mbps(bps)} of {port.speed_mbps} Mbps, {window}).",
                               "A port this busy adds delay and drops packets in bursts, which shows up as I/O "
                               "connection timeouts. Look for a camera or PC streaming on the machine network, "
                               "a 10/100 link that should be gigabit, or a loop."))
    b = tr.in_bcast_pps
    if b is not None and b >= t.bcast_pps:
        storm = b >= t.bcast_storm_pps
        out.append(Finding("error" if storm else "warning", "port.broadcast_storm", where,
                           f"{_rate(b)} broadcast packets/s {source} ({window}).",
                           "Healthy devices send a few broadcasts a second (ARP, DHCP). This many usually means a loop "
                           "(an unmanaged switch or a ring cabled twice), a faulty NIC, or a PC flooding discovery. "
                           "Unplug what is on this port to confirm; storm-control on the port limits the damage." + beyond))
    m = tr.in_mcast_pps
    if m is not None and m >= t.mcast_pps:
        out.append(Finding("info", "port.multicast_high", where,
                           f"{_rate(m)} multicast packets/s {source} ({window}).",
                           "EtherNet/IP multicast I/O at a fast RPI can be this busy. If this device isn't producing "
                           "multicast I/O, look for a camera or PC streaming, or switch its connections to unicast." + beyond))
    m = tr.out_mcast_pps
    if m is not None and m >= t.mcast_flood_pps and not port.is_uplink:
        out.append(Finding("info", "port.multicast_flood", where,
                           f"The switch sends {_rate(m)} multicast packets/s to this device ({window}).",
                           "Fine if the device consumes that multicast I/O. Otherwise IGMP snooping is off or no IGMP "
                           "querier is running on this VLAN, so every device gets every multicast packet."))
    e = tr.in_errors_ps
    if e is not None and e >= t.errors_per_s:
        out.append(Finding("warning", "port.errors_rising", where,
                           f"Input errors are climbing now: {_rate(e)} per second ({window}).",
                           "The cable, connector or device NIC is failing right now, or a duplex mismatch. Re-terminate "
                           "or swap the patch cable, and keep it away from VFD output and motor leads."))
    d = tr.out_discards_ps
    if d is not None and d >= t.drops_per_s:
        out.append(Finding("warning", "port.drops", where,
                           f"The switch is dropping {_rate(d)} packets/s it should send on this port ({window}).",
                           "Output drops mean more traffic is headed to this port than the link can take. Check the "
                           "link speed, a storm elsewhere on the VLAN, or a busy device sharing this port."))
    return out


def _ago(seconds: int) -> str:
    return f"{seconds} s" if seconds < 120 else f"{seconds // 60} min"


def load_baseline(path: str | Path) -> dict[str, Any]:
    """Firmware baseline file, see docs/baseline.example.json."""
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def _parse_rev(rev: str) -> tuple[int, int]:
    major, _, minor = str(rev).partition(".")
    return int(major), int(minor or 0)


def run_diagnostics(
    identities: list[DiscoveredIdentity],
    devices: list[Device],
    switches: list[SwitchInfo],
    *,
    baseline: Optional[dict[str, Any]] = None,
    thresholds: Optional[Thresholds] = None,
) -> list[Finding]:
    t = thresholds or Thresholds()
    findings: list[Finding] = []
    add = findings.append

    # ---------------------------------------------------------- IP addressing
    by_ip: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for d in identities:
        by_ip[d.source_ip].add((d.identity.vendor_id, d.identity.serial))
        sock_ip = d.identity.socket_ip
        if sock_ip and sock_ip not in ("0.0.0.0", d.source_ip):
            add(Finding("warning", "ip.socket_mismatch", d.source_ip,
                        f"{d.identity.product_name} replied from {d.source_ip} but reports its address as {sock_ip}.",
                        "Usually NAT (e.g. 1783-NATR / Stratix NAT) or a stale IP configuration in the device."))
    for ip, ids in by_ip.items():
        if len(ids) > 1:
            add(Finding("error", "ip.duplicate", ip,
                        f"{len(ids)} different devices answered from {ip}.",
                        "Duplicate IP address. Disconnect devices one at a time or check rotary switches/BOOTP."))

    serial_ips: dict[tuple[int, int], set[str]] = defaultdict(set)
    for d in identities:
        serial_ips[(d.identity.vendor_id, d.identity.serial)].add(d.source_ip)
    for (vendor, serial), ips in serial_ips.items():
        if len(ips) > 1 and serial != 0:
            add(Finding("info", "device.multi_ip", ", ".join(sorted(ips)),
                        f"Device serial {serial:08X} answers on {len(ips)} IPs.",
                        "Normal for multi-port modules (e.g. dual-IP adapters); otherwise check for cloned serials."))

    # ---------------------------------------------------------- CIP device health
    for dev in devices:
        ident = dev.identity
        if ident is None:
            continue
        target = dev.label
        if ident.status & (cip_tables.STATUS_MAJOR_RECOVERABLE | cip_tables.STATUS_MAJOR_UNRECOVERABLE) or ident.state in (4, 5):
            add(Finding("error", "cip.major_fault", target,
                        f"Major fault reported (state: {ident.state_name}; {ident.extended_status}).",
                        "Check the module status LED and fault log via its web page or Studio 5000."))
        elif ident.status & (cip_tables.STATUS_MINOR_RECOVERABLE | cip_tables.STATUS_MINOR_UNRECOVERABLE):
            add(Finding("warning", "cip.minor_fault", target, "Minor fault reported.",
                        "Often a configuration or I/O warning; check the device diagnostics page."))
        ext = (ident.status >> 4) & 0x0F
        if ext == 2:
            add(Finding("warning", "cip.io_faulted", target, "At least one I/O connection is faulted.",
                        "Check RPI, electronic keying and that the owning controller's I/O tree matches this device."))
        elif ext == 1:
            add(Finding("info", "cip.fw_update", target, "Firmware update in progress.", "Do not power cycle."))
        elif ext == 4:
            add(Finding("warning", "cip.nv_config_bad", target, "Non-volatile configuration is bad.",
                        "Reapply configuration or reset to factory defaults."))
        elif ext == 3 and ident.device_type not in (0x0E, 0x2C, 0x18):
            add(Finding("info", "cip.no_io", target, "No I/O connection established (not owned by a controller).",
                        "Expected if this device is not in a controller's I/O tree yet."))

    # ---------------------------------------------------------- firmware
    by_product: dict[tuple[int, int], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for dev in devices:
        # Rockwell Software entries are PCs running Linx: their "revision" is the Linx version.
        if dev.identity and dev.identity.vendor_id != cip_tables.VENDOR_ROCKWELL_SOFTWARE:
            by_product[(dev.identity.vendor_id, dev.identity.product_code)][dev.identity.revision].append(dev.ip or "?")
    for (_vendor, _code), revs in by_product.items():
        if len(revs) > 1:
            name = next(d.identity.product_name for d in devices
                        if d.identity and (d.identity.vendor_id, d.identity.product_code) == (_vendor, _code))
            detail = "; ".join(f"{r}: {', '.join(ips)}" for r, ips in sorted(revs.items()))
            add(Finding("info", "fw.mixed", name, f"Mixed firmware revisions for {name} ({detail}).",
                        "Fine if intentional; otherwise standardise via ControlFLASH."))
    if baseline:
        findings.extend(_check_baseline(devices, baseline))

    # ---------------------------------------------------------- switches / ports
    for sw in switches:
        name = sw.sys_name or sw.ip
        for err in sw.errors:
            add(Finding("warning", "switch.collect", name, f"Could not read {err}",
                        "Check SNMP credentials/ACLs. On IOS, MAC tables need community@vlan (v2c) or vlan-N context (v3) access."))
        if sw.uptime_seconds and sw.uptime_seconds < t.recent_reboot_seconds:
            add(Finding("info", "switch.recent_reboot", name, f"Switch has been up only {sw.uptime_seconds // 60} min.",
                        "Error counters were reset; MAC table may still be populating."))
        findings.extend(_switch_health(sw, name, t))
        for p in sw.ports:
            if not p.is_physical:
                continue
            where = f"{name} {p.name}"
            up = p.oper_status == "up"
            if up and p.duplex == "half":
                add(Finding("warning", "port.half_duplex", where, f"Port is running half duplex at {p.speed_mbps} Mbps.",
                            "Likely a duplex mismatch: set both ends to auto or hard-code both ends identically."))
            if p.late_collisions >= t.late_collisions:
                add(Finding("warning", "port.late_collisions", where, f"{p.late_collisions} late collisions.",
                            "Classic duplex mismatch symptom, or cable run too long."))
            errs = p.fcs_errors + p.alignment_errors + p.in_errors
            if errs >= t.port_errors:
                add(Finding("warning", "port.errors", where,
                            f"Input errors: FCS {p.fcs_errors}, alignment {p.alignment_errors}, total in {p.in_errors}.",
                            "Bad cable/connector, EMI near VFD/motor leads, or duplex mismatch. Re-scan to see if still increasing."))
            if up and 0 < p.speed_mbps <= 10:
                add(Finding("info", "port.10mbps", where, "Link negotiated at 10 Mbps.",
                            "Expected for some legacy devices; otherwise check cable/pairs."))
            if p.admin_status == "up" and p.oper_status == "down" and p.alias:
                add(Finding("info", "port.down", where, f"Port '{p.alias}' is down.",
                            "Device off, unplugged, or port err-disabled (check 'show interfaces status err-disabled')."))
            findings.extend(traffic_findings(where, p, t))
            if not p.is_uplink and len(p.macs) > t.macs_on_access_port:
                add(Finding("info", "port.multi_mac", where, f"{len(p.macs)} MAC addresses learned on this edge port.",
                            "Daisy-chained devices (embedded switch / DLR) or an unmanaged switch downstream."))

    # ---------------------------------------------------------- correlation gaps
    if switches:
        for dev in devices:
            if dev.identity and not dev.switch_port:
                if dev.mac:
                    add(Finding("info", "device.unlocated", dev.label,
                                f"MAC {dev.mac} not found in any scanned switch MAC table.",
                                "On an unscanned switch, behind NAT, or the MAC table entry aged out."))
                else:
                    add(Finding("info", "device.unlocated", dev.label,
                                "MAC address unknown, so the switch port can't be determined.",
                                "Device is routed (not on the scanner's subnet) and no scanned switch has it in its ARP table."))

    _fold_passing_storms(findings, switches)
    order = {s: i for i, s in enumerate(("error", "warning", "info"))}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.code, f.target))
    return findings


FOLDED_CODES = ("port.broadcast_storm", "port.multicast_high")


def _fold_passing_storms(findings: list[Finding], switches: list[SwitchInfo]) -> None:
    """A storm shows on its source port and on every switch-to-switch link it crosses. When the source port is in
    this scan, the links' findings become notes that point at it, so one storm reads as one problem."""
    uplinks = {f"{sw.sys_name or sw.ip} {p.name}" for sw in switches for p in sw.ports if p.is_uplink}
    for code in FOLDED_CODES:
        sources = [f.target for f in findings if f.code == code and f.target not in uplinks]
        if not sources:
            continue
        for f in findings:
            if f.code == code and f.target in uplinks:
                f.severity = "info"
                f.message += f" Same storm as {', '.join(sources)}, passing through this link."


def _check_baseline(devices: list[Device], baseline: dict[str, Any]) -> list[Finding]:
    """Baseline entries match by product_name, or by vendor_id + product_code."""
    out = []
    for rule in baseline.get("firmware", []):
        expected = rule.get("revision")
        if not expected:
            continue
        for dev in devices:
            ident = dev.identity
            if ident is None:
                continue
            if "product_name" in rule:
                match = ident.product_name.lower() == str(rule["product_name"]).lower()
            else:
                match = ident.vendor_id == rule.get("vendor_id") and ident.product_code == rule.get("product_code")
            if match and (ident.revision_major, ident.revision_minor) != _parse_rev(expected):
                out.append(Finding("warning", "fw.baseline", dev.label,
                                   f"Firmware {ident.revision} does not match baseline {expected}.",
                                   rule.get("note", "Update with ControlFLASH / ControlFLASH Plus.")))
    return out

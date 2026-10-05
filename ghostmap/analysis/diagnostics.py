"""Rule based findings that point a commissioning engineer at likely problems."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ghostmap.models import Device, DiscoveredIdentity, Finding, SwitchInfo
from ghostmap.protocols import cip_tables


@dataclass
class Thresholds:
    port_errors: int = 10  # FCS + alignment + input errors since counters were cleared
    late_collisions: int = 1
    macs_on_access_port: int = 1  # more than this on a non-uplink port is worth noting
    recent_reboot_seconds: int = 3600


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

    order = {s: i for i, s in enumerate(("error", "warning", "info"))}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.code, f.target))
    return findings


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

"""Correlate CIP identities, ARP data and switch MAC tables into a device inventory.

    IP --(ARP: local host + switches)--> MAC --(switch FDB)--> switch/port
    IP --(ListIdentity)--> vendor / product / firmware / serial / status
"""

from __future__ import annotations

from typing import Optional

from ghostmap.models import Device, DiscoveredIdentity, Port, SwitchInfo


def _ip_sort_key(ip: Optional[str]) -> tuple:
    if not ip:
        return (1, ())
    try:
        return (0, tuple(int(p) for p in ip.split(".")))
    except ValueError:
        return (0, (ip,))


def locate_macs(switches: list[SwitchInfo]) -> dict[str, tuple[SwitchInfo, Port]]:
    """MAC -> (switch, edge port).

    A MAC is visible on every switch between the scanner and the device; the
    real attachment point is the non-uplink port with the fewest MACs.
    """
    candidates: dict[str, list[tuple[SwitchInfo, Port]]] = {}
    for sw in switches:
        for port in sw.ports:
            for mac in port.macs:
                candidates.setdefault(mac, []).append((sw, port))
    located = {}
    for mac, options in candidates.items():
        edge = [o for o in options if not o[1].is_uplink] or options
        located[mac] = min(edge, key=lambda o: (o[1].is_uplink, len(o[1].macs)))
    return located


def build_inventory(
    identities: list[DiscoveredIdentity],
    switches: list[SwitchInfo],
    local_arp: Optional[dict[str, str]] = None,
    *,
    include_non_cip: bool = True,
) -> list[Device]:
    ip_to_mac: dict[str, str] = {}
    for sw in switches:
        ip_to_mac.update(sw.arp)
    ip_to_mac.update(local_arp or {})  # the scanner's own cache is the freshest
    switch_macs = {p.mac for sw in switches for p in sw.ports if p.mac}
    switch_ips = {sw.ip for sw in switches}
    located = locate_macs(switches)

    devices: dict[str, Device] = {}

    def place(dev: Device) -> None:
        if dev.mac and dev.mac in located:
            sw, port = located[dev.mac]
            dev.switch_ip, dev.switch_name, dev.switch_port, dev.vlan = sw.ip, sw.sys_name, port.name, port.vlan
            if "fdb" not in dev.sources:
                dev.sources.append("fdb")

    switch_by_ip = {sw.ip: sw for sw in switches}
    for d in identities:
        if d.source_ip in devices:
            continue  # duplicates are reported by diagnostics from the raw replies
        dev = Device(ip=d.source_ip, identity=d.identity, sources=["cip"])
        dev.mac = ip_to_mac.get(d.source_ip)
        if dev.mac:
            dev.sources.append("arp")
        if d.source_ip in switch_by_ip:  # a scanned switch answering ListIdentity for itself
            sw = switch_by_ip[d.source_ip]
            dev.switch_ip, dev.switch_name, dev.switch_port = sw.ip, sw.sys_name, "self"
        else:
            place(dev)
        devices[d.source_ip] = dev

    if include_non_cip:
        known_macs = {d.mac for d in devices.values() if d.mac}
        # Non-CIP hosts we can see an IP for (HMIs/PCs/cameras/etc).
        for ip, mac in ip_to_mac.items():
            if ip in devices or ip in switch_ips or mac in switch_macs or mac in known_macs:
                continue
            dev = Device(ip=ip, mac=mac, sources=["arp"])
            place(dev)
            if dev.switch_port:  # only keep hosts we can tie to the machine network
                devices[ip] = dev
                known_macs.add(mac)
        # MACs learned on an edge port with no IP at all - "something is plugged in here".
        for mac, (sw, port) in located.items():
            if mac in known_macs or mac in switch_macs or port.is_uplink:
                continue
            devices[f"mac:{mac}"] = Device(
                mac=mac, switch_ip=sw.ip, switch_name=sw.sys_name, switch_port=port.name,
                vlan=port.vlan, sources=["fdb"],
            )

    return sorted(devices.values(), key=lambda d: (_ip_sort_key(d.ip), d.mac or ""))

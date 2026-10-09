"""Correlate CIP identities, ARP data and switch MAC tables into a device inventory.

    IP --(ARP: local host + switches)--> MAC --(switch FDB)--> switch/port
    IP --(ListIdentity)--> vendor / product / firmware / serial / status
"""

from __future__ import annotations

from typing import Optional

from ghostmap.models import Device, DiscoveredIdentity, Port, SwitchInfo
from ghostmap.protocols.oui import mac_vendor


def _ip_sort_key(ip: Optional[str]) -> tuple:
    if not ip:
        return (1, ())
    try:
        return (0, tuple(int(p) for p in ip.split(".")))
    except ValueError:
        return (0, (ip,))


def _short(name: str) -> str:
    return name.split(".")[0].strip().lower()


def mark_inter_switch_links(switches: list[SwitchInfo]) -> None:
    """Mark ports that connect two scanned switches as uplinks.

    The collector can only guess from neighbour names. With several switches
    in one scan we know better: a port is a switch-to-switch link when its
    LLDP/CDP neighbour is another scanned switch (by name or IP), or when it
    has learned one of another scanned switch's own interface MACs (works
    even with LLDP and CDP disabled).
    """
    if len(switches) < 2:
        return
    for sw in switches:
        others = [o for o in switches if o is not sw]
        other_names = {_short(o.sys_name) for o in others if o.sys_name}
        other_ips = {o.ip for o in others}
        other_macs = {p.mac for o in others for p in o.ports if p.mac}
        for port in sw.ports:
            if port.is_uplink:
                continue
            if any(_short(n.remote_name) in other_names or n.remote_address in other_ips for n in port.neighbors):
                port.is_uplink = True
            elif other_macs.intersection(port.macs):
                port.is_uplink = True


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
    local_ips: frozenset[str] | set[str] = frozenset(),
) -> list[Device]:
    """``local_arp`` must already be limited to the swept subnet(s): every host in it
    answered ARP during the sweep, so it is listed even without a switch location.
    Hosts only known from a switch's ARP table (which on an L3 switch can cover the
    whole plant) are listed only when they sit on a scanned switch port.
    """
    mark_inter_switch_links(switches)
    local_arp = local_arp or {}
    ip_to_mac: dict[str, str] = {}
    for sw in switches:
        ip_to_mac.update(sw.arp)
    ip_to_mac.update(local_arp)  # the scanner's own cache is the freshest
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
            if ip in local_arp or dev.switch_port:
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

    for dev in devices.values():
        dev.mac_vendor = mac_vendor(dev.mac)
        dev.is_scanner = bool(dev.ip and dev.ip in local_ips)
    return sorted(devices.values(), key=lambda d: (_ip_sort_key(d.ip), d.mac or ""))

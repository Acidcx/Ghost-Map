"""Compare two scans: what was added, removed, moved or re-flashed."""

from __future__ import annotations

from typing import Any

from ghostmap.models import Device, ScanResult

_TRACKED = (
    ("ip", lambda d: d.ip),
    ("mac", lambda d: d.mac),
    ("location", lambda d: f"{d.switch_name or d.switch_ip} {d.switch_port}" if d.switch_port else None),
    ("vlan", lambda d: d.vlan),
    ("firmware", lambda d: d.identity.revision if d.identity else None),
    ("product", lambda d: d.identity.product_name if d.identity else None),
    ("state", lambda d: d.identity.state_name if d.identity else None),
    ("status", lambda d: d.identity.extended_status if d.identity else None),
)


def diff_scans(old: ScanResult, new: ScanResult) -> dict[str, Any]:
    old_map: dict[str, Device] = {d.key: d for d in old.devices}
    new_map: dict[str, Device] = {d.key: d for d in new.devices}

    added = [_brief(new_map[k]) for k in new_map.keys() - old_map.keys()]
    removed = [_brief(old_map[k]) for k in old_map.keys() - new_map.keys()]
    changed = []
    for key in old_map.keys() & new_map.keys():
        a, b = old_map[key], new_map[key]
        changes = {name: {"old": get(a), "new": get(b)} for name, get in _TRACKED if get(a) != get(b)}
        if changes:
            changed.append({**_brief(b), "changes": changes})

    # Same IP + same product, different serial: hardware was swapped.
    replaced = []
    for a in list(removed):
        old_dev = old_map[a["key"]]
        for b in list(added):
            new_dev = new_map[b["key"]]
            if (old_dev.ip and old_dev.ip == new_dev.ip and old_dev.identity and new_dev.identity
                    and old_dev.identity.product_name == new_dev.identity.product_name):
                replaced.append({**b, "old_serial": old_dev.identity.serial_hex, "new_serial": new_dev.identity.serial_hex,
                                 "old_firmware": old_dev.identity.revision, "new_firmware": new_dev.identity.revision})
                removed.remove(a)
                added.remove(b)
                break

    old_findings = {(f.code, f.target) for f in old.findings}
    new_findings = {(f.code, f.target) for f in new.findings}
    return {
        "old": old.id,
        "new": new.id,
        "added": sorted(added, key=lambda d: d["label"]),
        "removed": sorted(removed, key=lambda d: d["label"]),
        "changed": sorted(changed, key=lambda d: d["label"]),
        "replaced": sorted(replaced, key=lambda d: d["label"]),
        "new_findings": [f.__dict__ for f in new.findings if (f.code, f.target) not in old_findings],
        "resolved_findings": [f.__dict__ for f in old.findings if (f.code, f.target) not in new_findings],
    }


def _brief(d: Device) -> dict[str, Any]:
    return {"key": d.key, "label": d.label, "ip": d.ip, "mac": d.mac}

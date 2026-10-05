"""Scan persistence: one JSON file per scan in a data directory."""

from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path
from typing import Any

from ghostmap.models import ScanResult, from_dict, to_dict

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def default_data_dir() -> Path:
    return Path(os.environ.get("GHOSTMAP_DATA", Path.home() / ".ghostmap"))


class ScanStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_data_dir()
        self.scans = self.root / "scans"
        self.scans.mkdir(parents=True, exist_ok=True)

    def _path(self, scan_id: str) -> Path:
        if not _ID_RE.match(scan_id):
            raise KeyError(scan_id)
        return self.scans / f"{scan_id}.json"

    def save(self, scan: ScanResult) -> Path:
        path = self._path(scan.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(to_dict(scan), indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, scan_id: str) -> ScanResult:
        path = self._path(scan_id)
        if not path.is_file():
            raise KeyError(scan_id)
        return load_scan_file(path)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.scans.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            findings = data.get("findings", [])
            out.append({
                "id": data.get("id", path.stem),
                "started": data.get("started"),
                "finished": data.get("finished"),
                "label": data.get("params", {}).get("label", ""),
                "devices": len(data.get("devices", [])),
                "switches": len(data.get("switches", [])),
                "errors": sum(1 for f in findings if f.get("severity") == "error"),
                "warnings": sum(1 for f in findings if f.get("severity") == "warning"),
            })
        return out

    def delete(self, scan_id: str) -> None:
        self._path(scan_id).unlink(missing_ok=True)


def load_scan_file(path: str | Path) -> ScanResult:
    return from_dict(ScanResult, json.loads(Path(path).read_text(encoding="utf-8")))


INVENTORY_COLUMNS = (
    "ip", "mac", "mac_vendor", "vendor", "product", "device_type", "product_code", "firmware", "serial",
    "state", "status", "switch", "port", "vlan", "sources", "note",
)


def inventory_rows(scan: ScanResult) -> list[dict[str, Any]]:
    rows = []
    for d in scan.devices:
        i = d.identity
        rows.append({
            "ip": d.ip or "",
            "mac": d.mac or "",
            "mac_vendor": d.mac_vendor,
            "vendor": i.vendor_name if i else "",
            "product": i.product_name if i else "",
            "device_type": i.device_type_name if i else "",
            "product_code": i.product_code if i else "",
            "firmware": i.revision if i else "",
            "serial": i.serial_hex if i else "",
            "state": i.state_name if i else "",
            "status": i.extended_status if i else "",
            "switch": d.switch_name or d.switch_ip or "",
            "port": d.switch_port or "",
            "vlan": d.vlan if d.vlan is not None else "",
            "sources": "+".join(d.sources),
            "note": "this computer (scanner)" if d.is_scanner else "",
        })
    return rows


def inventory_csv(scan: ScanResult) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=INVENTORY_COLUMNS)
    writer.writeheader()
    writer.writerows(inventory_rows(scan))
    return buf.getvalue()

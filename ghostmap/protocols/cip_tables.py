"""Lookup tables and decoders for the CIP Identity object.

The vendor table is intentionally small; drop a JSON file of
``{"<vendor id>": "<name>"}`` at ``ghostmap/data/vendors.json`` (or point
``GHOSTMAP_VENDORS`` at one) to extend it with the full ODVA list.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_BUILTIN_VENDORS: dict[int, str] = {
    1: "Rockwell Automation/Allen-Bradley",
    24: "ODVA",
    26: "Festo",
    40: "WAGO",
    43: "Balluff",
    48: "Turck",
    57: "Pepperl+Fuchs",
    77: "Rockwell Software",
    90: "HMS Networks",
    108: "Beckhoff Automation",
    243: "Schneider Electric",
    283: "Hilscher",
    309: "ProSoft Technology",
    322: "ifm electronic",
    345: "Endress+Hauser",
    356: "FANUC Robotics America",
    367: "Keyence",
}

# CIP device profiles (Identity attribute 2, "Device Type").
DEVICE_TYPES: dict[int, str] = {
    0x00: "Generic Device",
    0x02: "AC Drive",
    0x03: "Motor Overload",
    0x04: "Limit Switch",
    0x05: "Inductive Proximity Switch",
    0x06: "Photoelectric Sensor",
    0x07: "General Purpose Discrete I/O",
    0x09: "Resolver",
    0x0C: "Communications Adapter",
    0x0E: "Programmable Logic Controller",
    0x10: "Position Controller",
    0x13: "DC Drive",
    0x15: "Contactor",
    0x16: "Motor Starter",
    0x17: "Soft Start",
    0x18: "Human-Machine Interface",
    0x1A: "Mass Flow Controller",
    0x1B: "Pneumatic Valve",
    0x1C: "Vacuum Pressure Gauge",
    0x22: "Encoder",
    0x23: "Safety Discrete I/O",
    0x25: "CIP Motion Drive",
    0x2B: "Generic Device (keyable)",
    0x2C: "Managed Ethernet Switch",
}

DEVICE_TYPE_MANAGED_SWITCH = 0x2C
VENDOR_ROCKWELL_SOFTWARE = 77  # RSLinx Classic / FactoryTalk Linx answering for a PC; product name = hostname

# Identity attribute 8, "State".
STATES: dict[int, str] = {
    0: "Nonexistent",
    1: "Device Self Testing",
    2: "Standby",
    3: "Operational",
    4: "Major Recoverable Fault",
    5: "Major Unrecoverable Fault",
    255: "Default (not reported)",
}

# Identity attribute 5, "Status" - bits 4..7 extended device status.
EXTENDED_STATUS: dict[int, str] = {
    0: "Self-testing or unknown",
    1: "Firmware update in progress",
    2: "At least one faulted I/O connection",
    3: "No I/O connections established",
    4: "Non-volatile configuration bad",
    5: "Major fault",
    6: "At least one I/O connection in run mode",
    7: "At least one I/O connection established, all in idle mode",
}

STATUS_OWNED = 0x0001
STATUS_CONFIGURED = 0x0004
STATUS_MINOR_RECOVERABLE = 0x0100
STATUS_MINOR_UNRECOVERABLE = 0x0200
STATUS_MAJOR_RECOVERABLE = 0x0400
STATUS_MAJOR_UNRECOVERABLE = 0x0800

_STATUS_FLAGS = (
    (STATUS_OWNED, "owned"),
    (STATUS_CONFIGURED, "configured"),
    (STATUS_MINOR_RECOVERABLE, "minor_recoverable_fault"),
    (STATUS_MINOR_UNRECOVERABLE, "minor_unrecoverable_fault"),
    (STATUS_MAJOR_RECOVERABLE, "major_recoverable_fault"),
    (STATUS_MAJOR_UNRECOVERABLE, "major_unrecoverable_fault"),
)


@lru_cache(maxsize=1)
def _vendors() -> dict[int, str]:
    vendors = dict(_BUILTIN_VENDORS)
    candidates = [Path(__file__).resolve().parent.parent / "data" / "vendors.json"]
    if os.environ.get("GHOSTMAP_VENDORS"):
        candidates.append(Path(os.environ["GHOSTMAP_VENDORS"]))
    for path in candidates:
        if path.is_file():
            with path.open(encoding="utf-8") as fh:
                vendors.update({int(k): str(v) for k, v in json.load(fh).items()})
    return vendors


def vendor_name(vendor_id: int) -> str:
    return _vendors().get(vendor_id, f"Vendor {vendor_id}")


def device_type_name(device_type: int, vendor_id: int = 0) -> str:
    if vendor_id == VENDOR_ROCKWELL_SOFTWARE:
        return "Workstation (RSLinx / FactoryTalk Linx)"
    return DEVICE_TYPES.get(device_type, f"Device type 0x{device_type:02X}")


def state_name(state: int) -> str:
    return STATES.get(state, f"State {state}")


def decode_status(status: int) -> tuple[list[str], str]:
    """Return (flag names, extended status text) for an Identity status word."""
    flags = [name for bit, name in _STATUS_FLAGS if status & bit]
    extended = EXTENDED_STATUS.get((status >> 4) & 0x0F, f"Vendor specific ({(status >> 4) & 0x0F})")
    return flags, extended

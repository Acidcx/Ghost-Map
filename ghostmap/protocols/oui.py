"""MAC address -> manufacturer, from the IEEE OUI registry (MA-L, 24-bit prefixes).

Bundled as ``ghostmap/data/oui.tsv.gz`` (generated from the IEEE registry via
Wireshark's manuf data) so it works offline. Helps identify hosts that don't
speak EtherNet/IP: Moxa / Siemens / Phoenix switches, PCs, cameras, ...
"""

from __future__ import annotations

import gzip
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "oui.tsv.gz"


@lru_cache(maxsize=1)
def _table() -> dict[str, str]:
    if not _PATH.is_file():
        return {}
    with gzip.open(_PATH, "rt", encoding="utf-8") as fh:
        return dict(line.rstrip("\n").split("\t", 1) for line in fh if "\t" in line)


def mac_vendor(mac: str | None) -> str:
    """Manufacturer for a MAC like ``00:90:e8:12:34:56``, or "" if unknown / locally administered."""
    if not mac:
        return ""
    hexdigits = mac.replace(":", "").replace("-", "").replace(".", "").upper()
    if len(hexdigits) < 6:
        return ""
    if int(hexdigits[1], 16) & 0x2:  # locally administered (randomised / virtual)
        return ""
    return _table().get(hexdigits[:6], "")

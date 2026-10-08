"""Ghost Map's own log file, and the debug bundle an engineer can send when something goes wrong.

The log lives in ``<data_dir>/logs/ghostmap.log`` (rotated at 1 MB, 5 files kept). It records server errors
with their stack traces, OPC UA client warnings (session drops, keep-alive failures), dashboard connection
events and errors reported by the browser. It holds no tag values. It does hold gateway addresses and
user names, so look it over before sending it outside the plant.
"""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import platform
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

LOG_NAME = "ghostmap.log"
MAX_BYTES = 1_000_000
BACKUPS = 5
# Loggers whose records go to the file: ours, the OPC UA client, asyncio's "exception never retrieved"
# reports, and uvicorn's unhandled-exception reports.
LOGGERS = {"ghostmap": logging.INFO, "asyncua": logging.WARNING, "asyncio": logging.WARNING,
           "uvicorn.error": logging.WARNING}
_MARK = "_ghostmap_file"


def log_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "logs"


def setup(data_dir: Path) -> Path:
    """Send log records to ``<data_dir>/logs/ghostmap.log``. Safe to call more than once."""
    path = log_dir(data_dir) / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8",
                                                   delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _MARK, True)
    for name, level in LOGGERS.items():
        lg = logging.getLogger(name)
        for h in [h for h in lg.handlers if getattr(h, _MARK, False)]:
            lg.removeHandler(h)
            h.close()
        lg.addHandler(handler)
        lg.setLevel(level)
        # asyncua's own warnings go to the file only; they used to be printed on the console as
        # "unhandled" tracebacks when a session dropped.
        lg.propagate = name not in ("asyncua", "asyncio")
    return path


def tail(data_dir: Path, lines: int = 300) -> str:
    """The last ``lines`` lines of the log (current file, plus the previous one if needed)."""
    d = log_dir(data_dir)
    files = [d / LOG_NAME] + [d / f"{LOG_NAME}.{i}" for i in range(1, BACKUPS + 1)]
    out: list[str] = []
    for f in files:
        if len(out) >= lines:
            break
        try:
            out = f.read_text(encoding="utf-8", errors="replace").splitlines() + out
        except OSError:
            continue
    return "\n".join(out[-lines:])


def bundle(data_dir: Path, info: dict, extra: Optional[dict] = None) -> bytes:
    """A zip with the log files and ``info.json`` (versions, platform, comms health). No tag values."""
    import importlib.metadata as md

    def version(pkg):
        try:
            return md.version(pkg)
        except md.PackageNotFoundError:
            return None

    meta = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "python": sys.version, "platform": platform.platform(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "packages": {p: version(p) for p in ("asyncua", "fastapi", "uvicorn", "pysnmp", "cryptography", "python-tds", "pyOpenSSL")},
            **info, **(extra or {})}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.json", json.dumps(meta, indent=1, default=str))
        for f in sorted(log_dir(data_dir).glob(f"{LOG_NAME}*")):
            z.write(f, f"logs/{f.name}")
    return buf.getvalue()

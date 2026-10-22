"""TSC's own event logs, read-only, from the folders TSC writes them to on the HMI.

TSC has written them to two places over the years: ``C:\\Bradbury EventLogs`` now, and
``%APPDATA%\\Bradbury\\Event Logs`` of the Windows user that runs TSC before that. The service runs as LocalSystem,
whose %APPDATA% is not that user's, so the legacy folder is looked for in every profile under ``C:\\Users``.

Ghost Map only lists and reads files inside those folders. A file is named by the index of its folder and its path
relative to it, and anything that resolves outside the folder (``..``, a link) is refused.
"""

from __future__ import annotations

import codecs
import glob
import os
import re
import time
from pathlib import Path
from typing import Sequence

FOLDERS = (("TSC event logs", r"C:\Bradbury EventLogs"),
           ("TSC event logs (legacy)", r"C:\Users\*\AppData\Roaming\Bradbury\Event Logs"))
MAX_FILES = 500          # newest first; older ones aren't listed
MAX_DEPTH = 3            # folders below each root (logs per month or per year)
MAX_LINES = 5000
TAIL_BYTES = 8 << 20     # how far back from the end a read looks (8 MB)
PROBLEM = re.compile(r"\b(error|exception|fail(ed|ure)?|fault|timeout|timed out|unable|denied|abort(ed)?)\b", re.I)


def roots(folders: Sequence[tuple[str, str]] = FOLDERS) -> list[dict]:
    """The log folders that exist, with a label each. A glob (the legacy folder) can match more than one."""
    out = []
    for label, pattern in folders:
        matches = sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern]
        for m in matches:
            if os.path.isdir(m):
                out.append({"label": label if len(matches) == 1 else f"{label}: {m}", "path": m})
    return out


def list_files(folders: Sequence[tuple[str, str]] = FOLDERS) -> dict:
    found, rs = [], roots(folders)
    for i, r in enumerate(rs):
        base = Path(r["path"])
        for dirpath, dirnames, filenames in os.walk(base):
            depth = len(Path(dirpath).relative_to(base).parts)
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                found.append({"id": f"{i}/{p.relative_to(base).as_posix()}", "name": name, "folder": r["label"],
                              "size": st.st_size, "modified": st.st_mtime})
    found.sort(key=lambda f: f["modified"], reverse=True)
    return {"folders": [{"label": r["label"], "path": r["path"]} for r in rs],
            "searched": [p for _, p in folders], "files": found[:MAX_FILES], "more": max(0, len(found) - MAX_FILES)}


def resolve(file_id: str, folders: Sequence[tuple[str, str]] = FOLDERS) -> Path:
    """The file a listed id names, or ValueError if it isn't a file inside one of the log folders."""
    idx, _, rel = (file_id or "").partition("/")
    rs = roots(folders)
    if not idx.isdigit() or int(idx) >= len(rs) or not rel:
        raise ValueError("unknown log file")
    base = Path(rs[int(idx)]["path"]).resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file():
        raise ValueError("unknown log file")
    return p


def decode(raw: bytes) -> str:
    """Logs from .NET and Windows tools are often UTF-16; everything else is read as UTF-8 (or Windows-1252)."""
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16", errors="replace")
    if len(raw) > 3 and raw[1:2] == b"\0" and raw[3:4] == b"\0":
        return raw.decode("utf-16-le", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read(path: Path, lines: int = 500, query: str = "", problems_only: bool = False) -> dict:
    """The last ``lines`` lines (after filtering), newest at the bottom. Only the file's last 8 MB are looked at."""
    lines = min(max(int(lines), 1), MAX_LINES)
    size = path.stat().st_size
    with open(path, "rb") as f:  # TSC may be writing to it; reading doesn't get in its way
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
        raw = f.read()
    text = decode(raw)
    rows = text.splitlines()
    if size > TAIL_BYTES and rows:
        rows = rows[1:]  # the first line was cut by the seek
    q = query.strip().lower()
    if q:
        rows = [r for r in rows if q in r.lower()]
    if problems_only:
        rows = [r for r in rows if PROBLEM.search(r)]
    total = len(rows)
    rows = rows[-lines:]
    return {"name": path.name, "size": size, "modified": path.stat().st_mtime, "matched": total,
            "truncated": size > TAIL_BYTES, "lines": [{"text": r[:4000], "problem": bool(PROBLEM.search(r))} for r in rows]}


def demo_folders(root: Path) -> list[tuple[str, str]]:
    """Sample logs for ``web --demo``, written once under the data folder."""
    d = Path(root) / "demo-tsc-logs"
    new, legacy = d / "Bradbury EventLogs", d / "Users" / "Bradbury" / "Event Logs"
    if not new.exists():
        new.mkdir(parents=True)
        legacy.mkdir(parents=True)
        (new / "TSC_2026-10-22.log").write_text("\n".join([
            "2026-10-22 06:00:02 INFO  Service started, line 1 (Sidewall)",
            "2026-10-22 06:00:03 INFO  Connected to SQL .\\SQL2022",
            "2026-10-22 06:14:51 INFO  Order 104233 job 1 downloaded to PLC (12 parts)",
            "2026-10-22 07:02:10 WARN  PLC handshake slow (2.4 s)",
            "2026-10-22 07:02:15 ERROR Timeout waiting for Complete_FinishedPart from 172.16.0.10",
            "2026-10-22 07:02:16 INFO  Retrying part 8812",
            "2026-10-22 07:02:17 INFO  Part 8812 completed",
        ]) + "\n", encoding="utf-8")
        (new / "TSC_2026-10-21.log").write_text("2026-10-21 06:00:01 INFO  Service started\n", encoding="utf-8")
        (legacy / "EventLog_2025.txt").write_text("2025-12-19 15:20:44 Error: Unable to open coil file\r\n",
                                                  encoding="utf-16")
        now = time.time()
        os.utime(new / "TSC_2026-10-21.log", (now - 86400, now - 86400))
        os.utime(legacy / "EventLog_2025.txt", (now - 300 * 86400, now - 300 * 86400))
    return [("TSC event logs", str(new)), ("TSC event logs (legacy)", str(legacy))]


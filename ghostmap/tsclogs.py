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
                found.append({"id": f"{i}/{p.relative_to(base).as_posix()}", "name": name, "folder": r["label"], "date": file_date(name),
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


# One entry: "2026-10-19 14:14:13.722|Bradbury.TSC.DataView|<ip>|<pc>|Error|GetViewTableName|Object reference ...".
# Lines that don't start with a timestamp (a stack trace, a long message) belong to the entry above them.
ENTRY = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\|")
FIELDS = ("time", "source", "ip", "host", "level", "method", "message")
BAD_LEVELS = {"error", "fatal", "critical", "exception"}
DATE_IN_NAME = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")
TOP = 8                  # most repeated messages in the summary


def file_date(name: str) -> str:
    """YYYY-MM-DD from a daily log's name (2026-10-19.log, TSC_20261019.txt), or ''."""
    m = DATE_IN_NAME.search(name)
    return f"{m[1]}-{m[2]}-{m[3]}" if m and "01" <= m[2] <= "12" and "01" <= m[3] <= "31" else ""


def parse(rows: list[str]) -> tuple[str, list[dict]]:
    """TSC entries (with their continuation lines under ``more``), or one entry per line for any other format."""
    starts = sum(1 for r in rows if ENTRY.match(r))
    if not starts or starts * 4 < sum(1 for r in rows if r.strip()):
        return "plain", [{"message": r, "more": [], "problem": bool(PROBLEM.search(r))} for r in rows if r.strip()]
    out: list[dict] = []
    for r in rows:
        if ENTRY.match(r):
            parts = r.split("|", len(FIELDS) - 1)
            parts += [""] * (len(FIELDS) - len(parts))
            e = dict(zip(FIELDS, (x.strip() for x in parts)))
            e["more"] = []
            out.append(e)
        elif out:
            if r.strip() or out[-1]["more"]:
                out[-1]["more"].append(r)
        elif r.strip():
            out.append({"message": r, "more": []})  # the tail read started inside an entry
    for e in out:
        while e["more"] and not e["more"][-1].strip():
            e["more"].pop()
        e["problem"] = e.get("level", "").lower() in BAD_LEVELS or (not e.get("level") and bool(PROBLEM.search(e["message"])))
    return "tsc", out


def _hit(e: dict, q: str) -> bool:
    return any(q in str(v).lower() for k, v in e.items() if k not in ("more", "problem")) or \
        any(q in m.lower() for m in e["more"])


def read(path: Path, lines: int = 500, query: str = "", problems_only: bool = False,
         hide: Sequence[str] = (), source: str = "") -> dict:
    """The last ``lines`` entries that pass the filters, newest at the bottom, with a summary of the whole file
    (counts per level, sources, the most repeated messages). Only the file's last 8 MB are looked at."""
    lines = min(max(int(lines), 1), MAX_LINES)
    size = path.stat().st_size
    with open(path, "rb") as f:  # TSC may be writing to it; reading doesn't get in its way
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
        raw = f.read()
    rows = decode(raw).splitlines()
    if size > TAIL_BYTES and rows:
        rows = rows[1:]  # the first line was cut by the seek
    fmt, entries = parse(rows)
    count: dict[str, int] = {}
    repeats: dict[tuple, dict] = {}
    for e in entries:
        lv = e.get("level") or ""
        count[lv] = count.get(lv, 0) + 1
        if e.get("level"):
            k = (e.get("source"), e.get("method"), e["message"])
            r = repeats.setdefault(k, {"source": k[0], "method": k[1], "message": k[2][:300], "level": lv, "count": 0,
                                       "first": e.get("time"), "last": e.get("time")})
            r["count"] += 1
            r["last"] = e.get("time")
    hidden = {x.lower() for x in hide if x}
    q = query.strip().lower()
    hits = [e for e in entries
            if (e.get("level") or "").lower() not in hidden
            and (not source or e.get("source") == source)
            and (not problems_only or e["problem"])
            and (not q or _hit(e, q))]
    shown = hits[-lines:]
    for e in shown:
        e["message"] = e["message"][:4000]
        e["more"] = [m[:4000] for m in e["more"][:500]]
    top = sorted((r for r in repeats.values() if r["count"] > 1), key=lambda r: -r["count"])[:TOP]
    return {"name": path.name, "date": file_date(path.name), "size": size, "modified": path.stat().st_mtime,
            "truncated": size > TAIL_BYTES, "format": fmt, "total": len(entries), "matched": len(hits),
            "levels": count, "sources": sorted({e["source"] for e in entries if e.get("source")}), "top": top,
            "entries": shown}


def demo_folders(root: Path) -> list[tuple[str, str]]:
    """Sample daily logs in TSC's format for ``web --demo``, written once under the data folder."""
    d = Path(root) / "demo-tsc-logs"
    new, legacy = d / "Bradbury EventLogs", d / "Users" / "Bradbury" / "Event Logs"
    if not new.exists():
        new.mkdir(parents=True)
        legacy.mkdir(parents=True)
        now = time.time()
        for back in range(3):
            day = time.strftime("%Y-%m-%d", time.localtime(now - back * 86400))
            (new / f"{day}.log").write_text(_demo_day(day, back), encoding="utf-8")
            os.utime(new / f"{day}.log", (now - back * 86400, now - back * 86400))
        (legacy / "EventLog_2025.txt").write_text("2025-12-19 15:20:44 Error: Unable to open coil file\r\n",
                                                  encoding="utf-16")
        os.utime(legacy / "EventLog_2025.txt", (now - 300 * 86400, now - 300 * 86400))
    return [("TSC event logs", str(new)), ("TSC event logs (legacy)", str(legacy))]


def _demo_day(day: str, back: int) -> str:
    def e(t, src, level, method, msg):
        return f"{day} {t}|Bradbury.TSC.{src}|192.0.2.10|HMI-DEMO|{level}|{method}|{msg}"

    rows = [e("06:00:02.114", "Service", "Info", "OnStart", "TSC service started (line 1, Sidewall)"),
            e("06:00:03.020", "Data", "Info", "Connect", "Connected to .\\SQL2022"),
            e("06:14:51.430", "Download", "Info", "SendJob", "Order 104233 job 1 sent to PLC (12 parts)"),
            e("07:02:10.002", "Plc", "Warning", "WaitHandshake", "PLC handshake slow (2.4 s)"),
            e("07:02:15.118", "Plc", "Error", "WaitHandshake", "Timeout waiting for Complete_FinishedPart"),
            "System.TimeoutException: The operation has timed out.",
            "   at Bradbury.TSC.Plc.Handshake.Wait(String tag, Int32 ms)",
            "   at Bradbury.TSC.Plc.PartComplete.Run()",
            e("07:02:16.200", "Plc", "Info", "Retry", "Retrying part 8812"),
            e("07:02:17.051", "Plc", "Info", "PartComplete", "Part 8812 completed")]
    for i in range(6 if back == 0 else 2):
        rows += [e(f"09:14:{10 + i:02d}.519", "DataView", "Error", "GetViewTableName", "Object reference not set to an instance of an object."),
                 e(f"09:14:{10 + i:02d}.521", "DataGridSchedule", "Error", "LoadDataGridView", "Object reference not set to an instance of an object.")]
    rows.append(e("14:30:00.000", "Service", "Info", "ShiftChange", "Shift 2 started"))
    return "\n".join(rows) + "\n"

"""Production totals per line and clock hour, kept by Ghost Map in ``<data_dir>/production.db``.

TSC is still the source: every ``SYNC_S`` the background sync (``web/tsc.py``) asks it for the finished parts of
each line summed by the hour they ended in, from the last hour it has minus ``REFRESH_H`` (so a part finished or
corrected late in TSC is picked up). The first sync goes back ``BACKFILL_DAYS``. What is kept here doesn't
depend on what TSC still holds, or on the SQL Server answering when someone looks: day and week totals come
from these hours. Rows are never pruned (one per line per hour that made something, a few thousand a year).

Times are TSC's own local clock time (naive, like its EndTime column); lengths are in TSC's units, converted to
feet when read with the unit from the settings.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DB_NAME = "production.db"
SYNC_S = 300.0
REFRESH_H = 6
BACKFILL_DAYS = 400
FMT = "%Y-%m-%d %H:00"
FIELDS = ("parts", "pieces", "length", "scrap_pieces", "run_s")
log = logging.getLogger("ghostmap.prodhistory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hours (line INTEGER NOT NULL, hour TEXT NOT NULL, parts INTEGER, pieces INTEGER, length REAL,
                                  scrap_pieces INTEGER, run_s REAL, PRIMARY KEY (line, hour));
CREATE TABLE IF NOT EXISTS lines (line INTEGER PRIMARY KEY, name TEXT, first_hour TEXT, synced_until TEXT, synced_at REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def hour_of(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def period_start(t: datetime, by: str, day_start: int = 0) -> datetime:
    """The start of the hour, production day or week (Monday) holding ``t``. Days start at ``day_start``
    o'clock (the first shift), so a night shift isn't split at midnight."""
    if by == "hour":
        return hour_of(t)
    d = (t - timedelta(hours=day_start)).replace(hour=0, minute=0, second=0, microsecond=0)
    if by == "week":
        d -= timedelta(days=d.weekday())
    return d + timedelta(hours=day_start)


def next_start(start: datetime, by: str) -> datetime:
    return start + {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7)}[by]


def rollup(hours: list[dict], by: str, count: int, now: datetime, day_start: int = 0,
           first_hour: Optional[datetime] = None, feet_per: float = 1 / 12) -> list[dict]:
    """``count`` periods ending with the one in progress, oldest first. ``hours``: rows with ``hour`` (datetime)
    and the totals. A period that began before Ghost Map's first hour is marked ``partial``."""
    cur = period_start(now, by, day_start)
    starts = [cur]
    for _ in range(count - 1):
        prev = period_start(starts[0] - timedelta(seconds=1), by, day_start)
        starts.insert(0, prev)
    out = [{"start": s, "end": next_start(s, by), "parts": 0, "pieces": 0, "length": 0.0, "scrap_pieces": 0, "run_s": 0.0}
           for s in starts]
    i = 0
    for h in sorted(hours, key=lambda r: r["hour"]):
        while i < len(out) and h["hour"] >= out[i]["end"]:
            i += 1
        if i == len(out):
            break
        if h["hour"] < out[i]["start"]:
            continue
        for k in FIELDS:
            out[i][k] += h.get(k) or 0
    rows = []
    for p in out:
        rows.append({"start": p["start"].timestamp(), "end": p["end"].timestamp(), "label": label(p["start"], by),
                     "parts": int(p["parts"]), "pieces": int(p["pieces"]), "feet": round(p["length"] * feet_per, 1),
                     "scrap_pieces": int(p["scrap_pieces"]), "prod_hours": round(p["run_s"] / 3600, 2),
                     "now": p["start"] == cur,
                     "partial": bool(first_hour and p["start"] < first_hour)})
    return rows


def label(start: datetime, by: str) -> str:
    if by == "hour":
        return start.strftime("%Y-%m-%d %H:00")
    if by == "day":
        return start.strftime("%a %Y-%m-%d")
    iso = start.isocalendar()
    return f"Week {iso[1]} ({start.strftime('%Y-%m-%d')})"


class ProductionHistory:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / DB_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # written by the sync's worker thread, read by API threads
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ensure_source(self, source: str) -> bool:
        """Forget everything copied from another TSC database (or the demo). True when it was cleared."""
        with self._lock, self._db:
            r = self._db.execute("SELECT value FROM meta WHERE key = 'source'").fetchone()
            if r and r["value"] == source:
                return False
            if r:
                log.warning("TSC source changed (%s -> %s): production history starts again", r["value"], source)
            self._db.execute("DELETE FROM hours")
            self._db.execute("DELETE FROM lines")
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('source', ?)", (source,))
            return r is not None

    def line(self, line: int) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM lines WHERE line = ?", (int(line),)).fetchone()
        return dict(r) if r else None

    def lines(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM lines ORDER BY line")]

    def sync_from(self, line: int, now: datetime) -> datetime:
        """The first hour the next sync should ask TSC for."""
        known = self.line(line)
        if known and known["synced_until"]:
            return datetime.strptime(known["synced_until"], FMT) - timedelta(hours=REFRESH_H)
        return hour_of(now - timedelta(days=BACKFILL_DAYS))

    def put(self, line: int, name: str, since: datetime, until: datetime, rows: list[dict]) -> int:
        """Replace the hours from ``since`` with what TSC said. ``rows``: {hour, parts, pieces, length,
        scrap_pieces, run_s}. Returns how many hours made something."""
        since_s, until_s = hour_of(since).strftime(FMT), hour_of(until).strftime(FMT)
        with self._lock, self._db:
            self._db.execute("DELETE FROM hours WHERE line = ? AND hour >= ?", (int(line), since_s))
            self._db.executemany(
                "INSERT OR REPLACE INTO hours (line, hour, parts, pieces, length, scrap_pieces, run_s) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(int(line), hour_of(r["hour"]).strftime(FMT), *(r.get(k) or 0 for k in FIELDS))
                 for r in rows if r.get("hour") and hour_of(r["hour"]) >= hour_of(since)])
            self._db.execute(
                "INSERT INTO lines (line, name, first_hour, synced_until, synced_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(line) DO UPDATE SET name = excluded.name, synced_until = excluded.synced_until, "
                "synced_at = excluded.synced_at, first_hour = MIN(COALESCE(lines.first_hour, excluded.first_hour), excluded.first_hour)",
                (int(line), name, since_s, until_s, time.time()))
        return len(rows)

    def hours(self, line: int, since: datetime, until: Optional[datetime] = None) -> list[dict]:
        args = [int(line), hour_of(since).strftime(FMT)]
        sql = "SELECT * FROM hours WHERE line = ? AND hour >= ?"
        if until:
            sql += " AND hour < ?"
            args.append(until.strftime(FMT))
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY hour", args).fetchall()
        return [{**dict(r), "hour": datetime.strptime(r["hour"], FMT)} for r in rows]

    def counts(self) -> dict:
        with self._lock:
            return {"hours": self._db.execute("SELECT COUNT(*) FROM hours").fetchone()[0],
                    "lines": self._db.execute("SELECT COUNT(*) FROM lines").fetchone()[0]}

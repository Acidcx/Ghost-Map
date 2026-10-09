"""Alarm history with first-out, in a SQLite file on the machine running Ghost Map.

``<data_dir>/history.db``. The background collector reads every dashboard about once a second and hands
each read to ``History.observe()``, which records:

- **events**: one row per alarm (or faulted motion axis) from when it went active until it cleared;
- **stops**: a run of alarms from the first one after a clean machine until the last one clears. The
  alarm(s) that started it are its **first-out**. Reads are about a second apart, so two alarms that go
  active within the same second are a tie: both are marked first-out and the stop says how many tied.
  A PLC with its own first-out logic is still the authority for faults closer together than that;
- **comms gaps**: reads that failed. Alarm states are frozen during a gap: nothing is started or cleared
  on data Ghost Map didn't get, and an alarm that cleared during a gap gets its end marked uncertain;
- **run changes**: the machine's running tag going on or off.

Rows are kept for ``RETENTION_DAYS``. Times are Unix seconds (UTC). Only Ghost Map writes this file; the
PLCs and gateways are only ever read.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from ghostmap.analysis.alarms import alarm_states, running_state

RETENTION_DAYS = 90
ALIVE_EVERY_S = 10.0    # how often "still recording" is noted, so a restart can close what was open
PRUNE_EVERY_S = 3600.0
DB_NAME = "history.db"
SCHEMA_VERSION = 2  # PRAGMA user_version; bump it with a migration when the tables change (2: meters)
METER_STEP_MAX_S = 10.0  # a longer gap between good reads is not counted (Ghost Map was off or comms were down)
log = logging.getLogger("ghostmap.history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, dash TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL, area TEXT, area_title TEXT,
    label TEXT, severity TEXT, node_id TEXT, start REAL NOT NULL, end REAL, stop INTEGER, first_out INTEGER NOT NULL DEFAULT 0,
    at_start INTEGER NOT NULL DEFAULT 0, end_uncertain INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS events_dash_start ON events (dash, start);
CREATE INDEX IF NOT EXISTS events_open ON events (dash, end);
CREATE TABLE IF NOT EXISTS stops (
    id INTEGER PRIMARY KEY, dash TEXT NOT NULL, start REAL NOT NULL, end REAL, alarms INTEGER NOT NULL DEFAULT 0,
    first_known INTEGER NOT NULL DEFAULT 1, tie INTEGER NOT NULL DEFAULT 1, was_running INTEGER,
    end_uncertain INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS stops_dash_start ON stops (dash, start);
CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY, dash TEXT NOT NULL, start REAL NOT NULL, end REAL, reason TEXT);
CREATE INDEX IF NOT EXISTS gaps_dash_start ON gaps (dash, start);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, dash TEXT NOT NULL, at REAL NOT NULL, running INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS runs_dash_at ON runs (dash, at);
CREATE TABLE IF NOT EXISTS alive (dash TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meters (dash TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, first REAL NOT NULL,
    updated REAL NOT NULL, PRIMARY KEY (dash, name));
CREATE TABLE IF NOT EXISTS meter_days (dash TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (dash, day, name));
"""
# Meters are lifetime totals (hour meters), kept forever: machine running time and time with good reads.
# meter_days holds the same per local calendar day, for "last 7 days" style windows.


class History:
    def __init__(self, data_dir, retention_days: int = RETENTION_DAYS):
        self.path = Path(data_dir) / DB_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_s = retention_days * 86400
        self._lock = threading.Lock()  # the collector writes on the event loop; API reads run in worker threads
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            ver = self._db.execute("PRAGMA user_version").fetchone()[0]
            if ver > SCHEMA_VERSION:
                log.warning("%s was written by a newer Ghost Map (schema %d, this one knows %d); recording anyway",
                            self.path, ver, SCHEMA_VERSION)
            elif ver < SCHEMA_VERSION:  # upgrades add columns or tables here, keeping the rows already recorded
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._close_after_restart()
            self._db.commit()
        # Per dashboard: open events {key: row id}, the open stop, the open gap, last run state.
        self._st: dict[str, dict] = {}
        self._pruned = 0.0

    def counts(self) -> dict:
        """Row counts and file size, for the debug bundle."""
        with self._lock:
            n = {t: self._db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("events", "stops", "gaps", "runs", "meters")}
            n["schema"] = self._db.execute("PRAGMA user_version").fetchone()[0]
        n["bytes"] = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.is_file())
        return n

    def close(self) -> None:
        with self._lock:
            now = time.time()
            for did, st in self._st.items():
                if st.get("acc"):
                    self._flush_meters(did, st, now)
            self._db.commit()
            self._db.close()

    # ----------------------------------------------------------------- recording
    def _close_after_restart(self) -> None:
        """Rows left open by the last run end when it was last known to be recording; marked uncertain."""
        alive = {r["dash"]: r["at"] for r in self._db.execute("SELECT dash, at FROM alive")}
        for table in ("events", "stops", "gaps"):
            for r in self._db.execute(f"SELECT id, dash, start FROM {table} WHERE end IS NULL").fetchall():
                end = max(alive.get(r["dash"], r["start"]), r["start"])
                extra = ", end_uncertain = 1" if table != "gaps" else ""
                self._db.execute(f"UPDATE {table} SET end = ?{extra} WHERE id = ?", (end, r["id"]))

    def _state(self, did: str) -> dict:
        st = self._st.get(did)
        if st is None:
            st = self._st[did] = {"open": {}, "stop": None, "stop_keys": set(), "gap": None, "running": None,
                                  "first": True, "alive": 0.0, "gap_cleared": set(), "last_ok": None, "acc": {}}
        return st

    def observe(self, dash: dict, result: dict, now: Optional[float] = None) -> dict:
        """Record one live read. Returns the dashboard's current stop: ``{"stop", "first_out": [keys]}``."""
        now = time.time() if now is None else now
        did = dash["id"]
        with self._lock:
            st = self._state(did)
            db = self._db
            if not result.get("ok"):
                if st["gap"] is None:
                    cur = db.execute("INSERT INTO gaps (dash, start, reason) VALUES (?, ?, ?)",
                                     (did, now, (result.get("error") or "")[:500]))
                    st["gap"] = cur.lastrowid
                    db.commit()
                return self._current(st)
            in_gap = st["gap"] is not None
            if in_gap:
                db.execute("UPDATE gaps SET end = ? WHERE id = ?", (now, st["gap"]))
                st["gap"] = None
            active, known = alarm_states(dash, result.get("values") or {}, result.get("bad") or [])
            changed = in_gap

            # Cleared: was open, now readable and not active. Unreadable ones stay open (state unknown).
            for key, row in list(st["open"].items()):
                if key in known and key not in active:
                    db.execute("UPDATE events SET end = ?, end_uncertain = ? WHERE id = ?", (now, int(in_gap), row))
                    del st["open"][key]
                    changed = True

            new = [k for k in active if k not in st["open"]]
            if new:
                changed = True
                if st["stop"] is None:
                    # A new stop. Alarms already on when recording started, or that came in during a comms gap,
                    # have no knowable first-out.
                    first_known = not st["first"] and not in_gap
                    cur = db.execute("INSERT INTO stops (dash, start, first_known, tie, was_running) VALUES (?, ?, ?, ?, ?)",
                                     (did, now, int(first_known), len(new) if first_known else 0,
                                      None if st["running"] is None else int(st["running"])))
                    st["stop"], st["stop_keys"] = cur.lastrowid, set()
                    first_out = set(new) if first_known else set()
                else:
                    first_out = set()
                for key in new:
                    a = active[key]
                    cur = db.execute(
                        "INSERT INTO events (dash, key, kind, area, area_title, label, severity, node_id, start, stop,"
                        " first_out, at_start) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (did, key, a["kind"], a["area"], a["area_title"], a["label"], a["severity"], a["node_id"], now,
                         st["stop"], int(key in first_out), int(st["first"])))
                    st["open"][key] = cur.lastrowid
                    st["stop_keys"].add(key)
                db.execute("UPDATE stops SET alarms = ? WHERE id = ?", (len(st["stop_keys"]), st["stop"]))

            if st["stop"] is not None and not st["open"]:
                db.execute("UPDATE stops SET end = ?, end_uncertain = ? WHERE id = ?", (now, int(in_gap), st["stop"]))
                st["stop"], st["stop_keys"] = None, set()
                changed = True

            # Hour meters: time between two good reads counts as online, and as running when the machine was
            # running at the first of them.
            if st["last_ok"] is not None and 0 < now - st["last_ok"] <= METER_STEP_MAX_S and not in_gap:
                dt = now - st["last_ok"]
                st["acc"]["online_s"] = st["acc"].get("online_s", 0.0) + dt
                if st["running"]:
                    st["acc"]["run_s"] = st["acc"].get("run_s", 0.0) + dt
            st["last_ok"] = now
            running = running_state(dash.get("layout") or {}, result.get("values") or {}, result.get("bad") or [])
            if running is not None and running != st["running"]:
                db.execute("INSERT INTO runs (dash, at, running) VALUES (?, ?, ?)", (did, now, int(running)))
                st["running"] = running
                changed = True

            st["first"] = False
            if now - st["alive"] >= ALIVE_EVERY_S:
                self._flush_meters(did, st, now)
                db.execute("INSERT INTO alive (dash, at) VALUES (?, ?) ON CONFLICT(dash) DO UPDATE SET at = excluded.at",
                           (did, now))
                st["alive"] = now
                changed = True
            if now - self._pruned >= PRUNE_EVERY_S:
                self._prune(now)
                changed = True
            if changed:
                db.commit()
            return self._current(st)

    def _flush_meters(self, did: str, st: dict, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        for name, v in st["acc"].items():
            self._db.execute("INSERT INTO meters (dash, name, value, first, updated) VALUES (?, ?, ?, ?, ?) "
                             "ON CONFLICT(dash, name) DO UPDATE SET value = value + excluded.value, updated = excluded.updated",
                             (did, name, v, now, now))
            self._db.execute("INSERT INTO meter_days (dash, day, name, value) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(dash, day, name) DO UPDATE SET value = value + excluded.value", (did, day, name, v))
        st["acc"] = {}

    def meters(self, did: str) -> dict:
        """Lifetime hour meters for a dashboard: {name: {"value": seconds, "first": when counting began}}, plus the
        part not written yet."""
        with self._lock:
            out = {r["name"]: {"value": r["value"], "first": r["first"]}
                   for r in self._db.execute("SELECT name, value, first FROM meters WHERE dash = ?", (did,))}
            for name, v in (self._st.get(did) or {}).get("acc", {}).items():
                out.setdefault(name, {"value": 0.0, "first": time.time()})["value"] += v
        return out

    def meter_window(self, did: str, name: str, days: int) -> float:
        """Seconds counted on a meter over the last ``days`` calendar days (today included)."""
        first = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
        with self._lock:
            v = self._db.execute("SELECT COALESCE(SUM(value), 0) FROM meter_days WHERE dash = ? AND name = ? AND day >= ?",
                                 (did, name, first)).fetchone()[0]
            return v + (self._st.get(did) or {}).get("acc", {}).get(name, 0.0)

    def _current(self, st: dict) -> dict:
        if st["stop"] is None:
            return {"stop": None, "first_out": []}
        rows = self._db.execute("SELECT key FROM events WHERE stop = ? AND first_out = 1", (st["stop"],)).fetchall()
        return {"stop": st["stop"], "first_out": [r["key"] for r in rows]}

    def current(self, did: str) -> dict:
        """The dashboard's open stop and its first-out keys (empty when the machine is clear)."""
        with self._lock:
            st = self._st.get(did)
            return self._current(st) if st else {"stop": None, "first_out": []}

    def _prune(self, now: float) -> None:
        cutoff = now - self.retention_s
        for table in ("events", "stops", "gaps"):
            self._db.execute(f"DELETE FROM {table} WHERE end IS NOT NULL AND end < ?", (cutoff,))
        self._db.execute("DELETE FROM runs WHERE at < ?", (cutoff,))
        self._pruned = now

    def forget(self, did: str) -> None:
        """Stop tracking a deleted dashboard (its rows stay until they age out)."""
        with self._lock:
            st = self._st.pop(did, None)
            if not st:
                return
            now = time.time()
            for row in st["open"].values():
                self._db.execute("UPDATE events SET end = ?, end_uncertain = 1 WHERE id = ?", (now, row))
            if st["stop"] is not None:
                self._db.execute("UPDATE stops SET end = ?, end_uncertain = 1 WHERE id = ?", (now, st["stop"]))
            if st["gap"] is not None:
                self._db.execute("UPDATE gaps SET end = ? WHERE id = ?", (now, st["gap"]))
            self._db.commit()

    # ----------------------------------------------------------------- reading
    def _rows(self, sql: str, args: tuple) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def events(self, did: str, since: float, until: Optional[float] = None, limit: int = 1000) -> list[dict]:
        """Alarm events that were active at any time in [since, until], newest first."""
        until = time.time() if until is None else until
        return self._rows("SELECT * FROM events WHERE dash = ? AND start <= ? AND (end IS NULL OR end >= ?)"
                          " ORDER BY start DESC, id DESC LIMIT ?", (did, until, since, limit))

    def stops(self, did: str, since: float, until: Optional[float] = None, limit: int = 500) -> list[dict]:
        """Stops in the window, newest first, each with its first-out alarm(s)."""
        until = time.time() if until is None else until
        stops = self._rows("SELECT * FROM stops WHERE dash = ? AND start <= ? AND (end IS NULL OR end >= ?)"
                           " ORDER BY start DESC, id DESC LIMIT ?", (did, until, since, limit))
        if stops:
            ids = [s["id"] for s in stops]
            marks = ",".join("?" * len(ids))
            firsts = self._rows(f"SELECT stop, key, label, area_title, severity FROM events WHERE stop IN ({marks})"
                                " AND first_out = 1 ORDER BY id", tuple(ids))
            by: dict[int, list] = {}
            for f in firsts:
                by.setdefault(f["stop"], []).append({k: f[k] for k in ("key", "label", "area_title", "severity")})
            for s in stops:
                s["first_out"] = by.get(s["id"], [])
        return stops

    def summary(self, did: str, since: float, until: Optional[float] = None, top: int = 15) -> dict:
        """Totals for the window: stops, alarm count, the alarms that fire most and those that cost the most time,
        comms gaps and how often each alarm was first-out."""
        until = time.time() if until is None else until
        evs = self.events(did, since, until, limit=200000)
        per: dict[str, dict] = {}
        for e in evs:
            p = per.setdefault(e["key"], {"key": e["key"], "label": e["label"], "area_title": e["area_title"],
                                          "severity": e["severity"], "count": 0, "seconds": 0.0, "first_out": 0})
            p["count"] += 1
            p["seconds"] += max(0.0, min(e["end"] or until, until) - max(e["start"], since))
            p["first_out"] += e["first_out"]
        for p in per.values():
            p["seconds"] = round(p["seconds"], 1)
        gaps = self._rows("SELECT start, end, reason FROM gaps WHERE dash = ? AND start <= ? AND (end IS NULL OR end >= ?)"
                          " ORDER BY start DESC", (did, until, since))
        stops = self._rows("SELECT COUNT(*) AS n FROM stops WHERE dash = ? AND start BETWEEN ? AND ?", (did, since, until))
        return {"since": since, "until": until, "alarms": len(evs), "stops": stops[0]["n"],
                "most_often": sorted(per.values(), key=lambda p: (-p["count"], -p["seconds"]))[:top],
                "longest": sorted(per.values(), key=lambda p: (-p["seconds"], -p["count"]))[:top],
                "first_out": sorted((p for p in per.values() if p["first_out"]), key=lambda p: -p["first_out"])[:top],
                "comms_gaps": len(gaps),
                "comms_gap_seconds": round(sum(min(g["end"] or until, until) - max(g["start"], since) for g in gaps), 1)}

    def events_csv(self, did: str, since: float, until: Optional[float] = None) -> str:
        """The alarm events as CSV (local times), for Excel."""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["start", "end", "seconds", "first_out", "area", "alarm", "severity", "kind", "node_id",
                    "stop_id", "start_unknown", "end_uncertain"])

        def fmt(t):
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
        for e in reversed(self.events(did, since, until, limit=200000)):
            w.writerow([fmt(e["start"]), fmt(e["end"]), round((e["end"] or time.time()) - e["start"], 1),
                        "yes" if e["first_out"] else "", e["area_title"], e["label"], e["severity"], e["kind"],
                        e["node_id"], e["stop"] or "", "yes" if e["at_start"] else "", "yes" if e["end_uncertain"] else ""])
        return out.getvalue()

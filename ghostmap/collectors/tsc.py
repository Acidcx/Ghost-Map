"""Read-only queries against the TSC (Total System Control) database on SQL Server.

Ghost Map reads the part schedule through TSC's own DataView views (``vPartScheduleCommon``, the queue view
``vHmiPartScheduleQueueOrInProgress`` with the run order in QueueIndex, and ``vPartScheduleCompleted``), and,
when the login is allowed to, a few lookup tables: stations and per-station progress, station dependencies,
the shift calendar, downtime with stop reasons, and the tooling tables used to count punch strokes. Every
query is a fixed SELECT; nothing else is ever sent (no INSERT, UPDATE, DELETE, EXEC or DDL), and the connection
asks for a read-only session (ApplicationIntent=ReadOnly). Give Ghost Map a SQL login that can only SELECT.
Customer names and comments are never read.

The driver is python-tds (pure Python), so GhostMap.exe needs no ODBC driver on the HMI. Encryption uses
pyOpenSSL: "trust server certificate" accepts the server's self-signed certificate, like the ODBC option of
the same name; otherwise give the CA file that signed it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

DEFAULT_VIEW = "DataView.vPartScheduleCommon"
DEFAULT_QUEUE_VIEW = "DataView.vHmiPartScheduleQueueOrInProgress"  # status 2, 3, 4 with QueueIndex
DEFAULT_DONE_VIEW = "DataView.vPartScheduleCompleted"              # status 5
# The columns the Production page uses. CustomerName and the comments are left out on purpose.
COLUMNS = ["PartID", "Quantity", "RequestedCoil", "OrderNumber", "Status", "StatusDescription", "RequestedQuantity", "QuantityAdjust",
           "ActualQuantity", "QuantityRemaining", "ProfileName", "StandardPartName", "Thickness", "StripWidth",
           "Tooling", "Color", "BundleMark", "Length", "PieceMark", "RecordXofY", "IsEndOfBundle", "ErrorCode",
           "ErrorCodeDescription", "RemakeCode", "IsScrap", "IsForcedRemake", "PriorityIndex", "ImportIndex",
           "StartTime", "EndTime", "LineID", "LineName", "Station1Status", "Station1ActualQuantity",
           "Station2Status", "Station2ActualQuantity"]
UNSET_BEFORE = datetime(1901, 1, 1)  # TSC writes 1900-01-01 for "no time yet"
_VIEW_RE = re.compile(r"^\[?[A-Za-z_][A-Za-z0-9_]*\]?\.\[?[A-Za-z_][A-Za-z0-9_]*\]?$")
_TRUST = "<trust-server-certificate>"


@dataclass
class TscConfig:
    server: str = ""            # host, host,port or host\INSTANCE
    database: str = ""
    username: str = ""
    password: str = field(default="", repr=False)
    encrypt: bool = False
    trust_cert: bool = True     # with encrypt: accept the server's self-signed certificate
    cafile: str = ""            # with encrypt and not trust_cert: CA certificate (PEM) to check it against
    view: str = DEFAULT_VIEW
    queue_view: str = DEFAULT_QUEUE_VIEW
    done_view: str = DEFAULT_DONE_VIEW
    length_unit: str = "in"     # unit of the Length column: in, ft or mm
    shifts: str = "06:00,18:00"  # shift start times, for "this shift"
    cache_s: int = 30           # how long a read is reused (several people watching cost one query)

    def check(self) -> None:
        if not self.server.strip():
            raise ValueError("server is required")
        if self.server.strip().lower() != "demo":
            if not self.database.strip():
                raise ValueError("database is required")
            if not self.username.strip():
                raise ValueError("username is required")
        for v in (self.view, self.queue_view, self.done_view):
            if not _VIEW_RE.match(v.strip()):
                raise ValueError("views must look like Schema.ViewName")
        if self.length_unit not in ("in", "ft", "mm"):
            raise ValueError("length unit must be in, ft or mm")
        parse_shifts(self.shifts)
        if not 5 <= int(self.cache_s) <= 3600:
            raise ValueError("cache must be 5 to 3600 seconds")


def parse_shifts(text: str) -> list[tuple[int, int]]:
    out = []
    for part in [p.strip() for p in str(text).split(",") if p.strip()]:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f"shift start {part!r} must look like 06:00")
        out.append((int(m.group(1)), int(m.group(2))))
    if not out:
        raise ValueError("at least one shift start time is needed, e.g. 06:00")
    return sorted(set(out))


def _split_server(server: str) -> tuple[str, Optional[int]]:
    s = server.strip()
    if "," in s:
        host, port = s.rsplit(",", 1)
        return host.strip(), int(port)
    return s, None


# Optional reads and the GRANT each needs; a read the login may not do is reported, not fatal.
FEATURES = {
    "stations": ("Station names, and progress at every station (3 and up)",
                 "GRANT SELECT ON dbo.Station TO <login>; GRANT SELECT ON dbo.StationPart TO <login>;"),
    "station_links": ("Stations that wait on others (e.g. pan + back skin)", "GRANT SELECT ON dbo.StationDependency TO <login>; "
                      "GRANT SELECT ON dbo.StationDependencyType TO <login>;"),
    "shifts": ("The shift calendar", "GRANT SELECT ON dbo.Shift TO <login>; GRANT SELECT ON dbo.DayOfWeek TO <login>;"),
    "downtime": ("Stops and their reasons", "GRANT SELECT ON dbo.vGetDownTimeData TO <login>;"),
    "strokes": ("Punch strokes per tool (maintenance)", "GRANT SELECT ON dbo.PartPattern TO <login>; GRANT SELECT ON dbo.PatternHole TO <login>; "
                "GRANT SELECT ON dbo.Hole TO <login>; GRANT SELECT ON dbo.PartNotch TO <login>;"),
    "tool_types": ("Tool names", "GRANT SELECT ON Machine.ToolType TO <login>;"),
}

# Holes one pattern puts on a part: once, or along the part every RepeatOffset between the lead and end offsets.
_HITS = ("CASE WHEN pp.IsRepeat = 1 AND pp.RepeatOffset > 0 THEN CASE WHEN p.Length - pp.LeadOffset - pp.EndOffset >= 0 "
         "THEN FLOOR((p.Length - pp.LeadOffset - pp.EndOffset) / pp.RepeatOffset) + 1 ELSE 0 END ELSE 1 END")


class SqlTsc:
    """The real source: python-tds, a short-lived connection per query batch."""

    name = "sql"

    def __init__(self, cfg: TscConfig):
        cfg.check()
        self.cfg = cfg
        self.view, self.queue_view, self.done_view = cfg.view.strip(), cfg.queue_view.strip(), cfg.done_view.strip()

    def _connect(self):
        import pytds

        host, port = _split_server(self.cfg.server)
        kw = dict(dsn=host, database=self.cfg.database, user=self.cfg.username, password=self.cfg.password,
                  login_timeout=10, timeout=30, autocommit=True, readonly=True, appname="Ghost Map (read-only)")
        if port:
            kw["port"] = port
        if self.cfg.encrypt:
            _install_trust_hook()
            kw["cafile"] = _TRUST if self.cfg.trust_cert or not self.cfg.cafile else self.cfg.cafile
            kw["validate_host"] = not self.cfg.trust_cert
        return pytds.connect(**kw)

    def _query(self, sql: str, args: tuple = ()) -> list[dict]:
        assert sql.lstrip().upper().startswith("SELECT"), "Ghost Map only reads"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                names = [d[0] for d in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------- the part schedule (DataView)
    def lines(self) -> list[dict]:
        return self._query(f"SELECT DISTINCT LineID, LineName FROM {self.view} ORDER BY LineID")

    def open_parts(self, line_id: int, limit: int = 300) -> list[dict]:
        """Queued, held and in-progress parts in the order the machine runs them."""
        return self._query(f"SELECT TOP {int(limit)} QueueIndex, {', '.join(COLUMNS)} FROM {self.queue_view} "
                           "WHERE LineID = %s ORDER BY QueueIndex", (int(line_id),))

    def done_parts(self, line_id: int, since: datetime, limit: int = 3000) -> list[dict]:
        return self._query(f"SELECT TOP {int(limit)} {', '.join(COLUMNS)} FROM {self.done_view} "
                           "WHERE LineID = %s AND EndTime >= %s ORDER BY EndTime DESC", (int(line_id), since))

    def order_totals(self, line_id: int, orders: list[str]) -> list[dict]:
        """Whole-order progress (every part of the order, not only the ones in view) for the given orders."""
        orders = [str(o) for o in orders][:200]
        if not orders:
            return []
        marks = ", ".join(["%s"] * len(orders))
        return self._query(
            "SELECT OrderNumber, COUNT(*) AS Parts, SUM(CASE WHEN Status = 5 THEN 1 ELSE 0 END) AS PartsDone, "
            "SUM(CAST(Quantity AS bigint)) AS Pieces, SUM(CAST(ActualQuantity AS bigint)) AS PiecesDone, "
            "COUNT(DISTINCT BundleID) AS Bundles, "
            "SUM(CASE WHEN Quantity > ActualQuantity THEN CAST(Length AS float) * (Quantity - ActualQuantity) ELSE 0 END) AS LengthLeft "
            f"FROM {self.view} WHERE LineID = %s AND OrderNumber IN ({marks}) GROUP BY OrderNumber",
            (int(line_id), *orders))

    def totals(self, line_id: int, since: datetime) -> dict:
        """Finished parts since a time, summed on the server: parts, pieces, length, scrap and run time."""
        rows = self._query(
            "SELECT COUNT(*) AS Parts, SUM(CAST(ActualQuantity AS bigint)) AS Pieces, "
            "SUM(CAST(Length AS float) * ActualQuantity) AS LengthTotal, "
            "SUM(CASE WHEN IsScrap = 1 THEN CAST(ActualQuantity AS bigint) ELSE 0 END) AS ScrapPieces, "
            "SUM(CASE WHEN StartTime > '19010101' AND EndTime > StartTime THEN CAST(DATEDIFF(second, StartTime, EndTime) AS bigint) ELSE 0 END) AS RunSeconds, "
            f"MIN(EndTime) AS FirstEnd FROM {self.done_view} WHERE LineID = %s AND EndTime >= %s", (int(line_id), since))
        return rows[0] if rows else {}

    # ------------------------------------------------------------- optional lookups (dbo / Machine)
    def station_parts(self, part_ids: list[str]) -> list[dict]:
        out = []
        ids = [str(i) for i in part_ids]
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            out += self._query("SELECT StationID, PartID, QuantityCompleted, IsInProgress, IsCompleted FROM dbo.StationPart "
                               f"WHERE PartID IN ({', '.join(['%s'] * len(chunk))})", tuple(chunk))
        return out

    def stations(self) -> list[dict]:
        return self._query("SELECT StationID, LineID, Name, Description FROM dbo.Station ORDER BY StationID")

    def station_links(self) -> list[dict]:
        return self._query("SELECT d.DependentStationID, d.IndependentStationID, t.Name AS TypeName FROM dbo.StationDependency d "
                           "LEFT JOIN dbo.StationDependencyType t ON t.StationDependencyTypeID = d.StationDependencyTypeID")

    def shifts(self) -> list[dict]:
        return self._query("SELECT s.ShiftID, s.Name, s.DayNumber, d.Name AS DayName, s.ShiftNumber, s.StartTime, s.EndTime "
                           "FROM dbo.Shift s LEFT JOIN dbo.DayOfWeek d ON d.DayNumber = s.DayNumber ORDER BY s.DayNumber, s.ShiftNumber")

    def downtime(self, line_id: int, since: datetime, limit: int = 500) -> list[dict]:
        """Stops that ended after ``since`` or haven't ended (StartTime is the restart; 1900 = still stopped)."""
        return self._query(f"SELECT TOP {int(limit)} LineID, StopTime, StartTime, StopCode, Description FROM dbo.vGetDownTimeData "
                           "WHERE LineID = %s AND (StartTime >= %s OR StartTime < '19010101') ORDER BY StopTime DESC",
                           (int(line_id), since))

    def tool_types(self) -> list[dict]:
        return self._query("SELECT HoleType, Description FROM Machine.ToolType")

    def strokes(self, line_id: int, since: datetime) -> list[dict]:
        """Punch strokes per station and tool type for parts finished since a time: pattern holes (repeats
        counted along the part), one-off holes and notches, times the pieces made."""
        q = int(line_id)
        return self._query(
            f"SELECT StationID, ToolType, SUM(Strokes) AS Strokes FROM ("
            f"SELECT ph.StationID, ph.ToolType, CAST(p.ActualQuantity AS bigint) * {_HITS} AS Strokes "
            f"FROM {self.done_view} p JOIN dbo.PartPattern pp ON pp.PartID = p.PartID "
            "JOIN dbo.PatternHole ph ON ph.PatternName = pp.PatternName WHERE p.LineID = %s AND p.EndTime >= %s "
            "UNION ALL SELECT h.StationID, h.ToolType, CAST(p.ActualQuantity AS bigint) "
            f"FROM {self.done_view} p JOIN dbo.Hole h ON h.PartID = p.PartID WHERE p.LineID = %s AND p.EndTime >= %s "
            "UNION ALL SELECT n.StationID, n.ToolType, CAST(p.ActualQuantity AS bigint) "
            f"FROM {self.done_view} p JOIN dbo.PartNotch n ON n.PartID = p.PartID WHERE p.LineID = %s AND p.EndTime >= %s"
            ") x GROUP BY StationID, ToolType", (q, since, q, since, q, since))


def source_for(cfg: TscConfig):
    if cfg.server.strip().lower() == "demo":
        from ghostmap.sim.tsc import SimTsc

        return SimTsc()
    return SqlTsc(cfg)


def test_connection(cfg: TscConfig) -> dict:
    """Connect, list the lines and time it. Used by the Test button."""
    t0 = time.perf_counter()
    lines = source_for(cfg).lines()
    return {"ok": True, "lines": lines, "ms": round((time.perf_counter() - t0) * 1000)}


_hooked = False


def _install_trust_hook() -> None:
    """Let python-tds accept the server's own certificate ("trust server certificate").

    python-tds builds its TLS context from a CA file; this swaps in a context that skips verification when
    the CA file is the trust marker, and leaves every other CA file as python-tds would have it."""
    global _hooked
    if _hooked:
        return
    import OpenSSL.SSL
    import pytds
    import pytds.tls

    original = pytds.tls.create_context

    def create_context(cafile):
        if cafile != _TRUST:
            return original(cafile)
        ctx = OpenSSL.SSL.Context(OpenSSL.SSL.TLSv1_2_METHOD)
        ctx.set_options(OpenSSL.SSL.OP_NO_SSLv2 | OpenSSL.SSL.OP_NO_SSLv3)
        ctx.set_verify(OpenSSL.SSL.VERIFY_NONE, lambda *a: True)
        return ctx

    pytds.tls.create_context = create_context
    _hooked = True

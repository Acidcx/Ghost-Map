"""Read-only queries against the TSC (Total System Control) part schedule on SQL Server.

Ghost Map reads one view, ``DataView.vPartScheduleCommon`` by default, with three fixed SELECTs: the lines,
a line's open parts (not finished yet) and a line's parts finished since a time. Nothing else is ever sent:
no INSERT, UPDATE, DELETE, EXEC or DDL, and the connection asks for a read-only session
(ApplicationIntent=ReadOnly). Give Ghost Map a SQL login that can only SELECT from the view's schema.

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
# The columns the Production page uses. CustomerName and the comments are left out on purpose.
COLUMNS = ["PartID", "OrderNumber", "Status", "StatusDescription", "RequestedQuantity", "QuantityAdjust",
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
        if not _VIEW_RE.match(self.view.strip()):
            raise ValueError("view must look like Schema.ViewName")
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


class SqlTsc:
    """The real source: python-tds, a short-lived connection per query batch."""

    name = "sql"

    def __init__(self, cfg: TscConfig):
        cfg.check()
        self.cfg = cfg
        self.view = cfg.view.strip()

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

    def lines(self) -> list[dict]:
        return self._query(f"SELECT DISTINCT LineID, LineName FROM {self.view} ORDER BY LineID")

    def open_parts(self, line_id: int, limit: int = 300) -> list[dict]:
        return self._query(f"SELECT TOP {int(limit)} {', '.join(COLUMNS)} FROM {self.view} "
                           "WHERE LineID = %s AND (EndTime IS NULL OR EndTime < %s) ORDER BY PriorityIndex, ImportIndex",
                           (int(line_id), UNSET_BEFORE))

    def done_parts(self, line_id: int, since: datetime, limit: int = 3000) -> list[dict]:
        return self._query(f"SELECT TOP {int(limit)} {', '.join(COLUMNS)} FROM {self.view} "
                           "WHERE LineID = %s AND EndTime >= %s ORDER BY EndTime DESC", (int(line_id), since))


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

"""Local web UI / JSON API.

Bound to 127.0.0.1 by default. Static assets are served from the package, so
the UI works on air-gapped OT networks (no CDNs). All URLs in the UI are
relative, so it also works under a path prefix behind the IXON HTTP proxy.

Access control (see ``auth.py``): an optional client allow-list, logins with
roles once any user account exists, a header check against cross-site
requests, and an audit log.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ghostmap import __version__, debuglog
from ghostmap.analysis.diff import diff_scans
from ghostmap.collectors import discovery
from ghostmap.models import to_dict
from ghostmap.protocols.snmp import SnmpCredentials
from ghostmap.scanner import ScanRequest, run_scan
from ghostmap.store import ScanStore, inventory_csv
from ghostmap.web.auth import AuditLog, Lockout, Sessions, UserStore, client_allowed
from ghostmap.history import RETENTION_DAYS, History
from ghostmap.web.collector import Collector
from ghostmap.web.dashboards import DashboardStore, LiveValues, visible_node_ids

UA_IDLE_S = 600        # close OPC UA sessions nobody has used for 10 minutes (re-opened on next use)
UA_FORGET_S = 8 * 3600  # forget them (and the login kept for re-opening) after 8 hours
UA_MAX_SESSIONS = 4
DEMO_UA_PORT = 4899    # not 4990, so the demo never collides with a real FT Linx Gateway on the HMI

CLIENT_LOG_PER_MIN = 30  # browser error reports accepted per client per minute
log = logging.getLogger("ghostmap.web")

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "gm_session"
CSRF_HEADER = "x-ghostmap"  # set by app.js on every request; a cross-site form can't add it
PUBLIC_PATHS = ("/login", "/api/login", "/static/")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    # style-src allows inline style attributes the UI builds; scripts are files only.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'",
}


class LoginIn(BaseModel):
    user: str = Field("", max_length=64)
    password: str = Field("", max_length=256)


class UserIn(BaseModel):
    name: str = Field(..., max_length=32)
    password: str = Field(..., max_length=256)
    role: str = Field("viewer", pattern="^(viewer|admin)$")


class UaConnectIn(BaseModel):
    url: str = Field(..., max_length=300)
    security: str = Field("None", max_length=40)
    mode: str = Field("SignAndEncrypt", pattern="^(Sign|SignAndEncrypt)$")
    username: str = Field("", max_length=128)
    password: str = Field("", max_length=256)


class UaUrlIn(BaseModel):
    url: str = Field(..., max_length=300)


class UaNodeIn(BaseModel):
    sid: str
    node_id: Optional[str] = Field(None, max_length=1000)


class UaReadIn(BaseModel):
    sid: str
    node_ids: list[str] = Field(..., max_length=200)


class ClientLogIn(BaseModel):
    message: str = Field("", max_length=2000)
    source: str = Field("", max_length=300)
    line: int = 0
    stack: str = Field("", max_length=4000)
    page: str = Field("", max_length=100)


class DashboardIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    endpoint: str = Field(..., max_length=300)
    source: str = Field("", max_length=1000)  # what was exported, e.g. the root node
    tags: Optional[list[dict]] = Field(None, max_length=250000)
    job: Optional[str] = Field(None, max_length=40)  # or: build from a finished Tag Browser export


class LayoutIn(BaseModel):
    layout: dict


class OverridesIn(BaseModel):
    invert: dict[str, bool] = {}
    hidden: list[str] = Field([], max_length=50000)
    name: Optional[str] = Field(None, max_length=80)


class SnmpIn(BaseModel):
    version: str = Field("2c", pattern="^(2c|3)$")
    community: str = ""
    username: str = ""
    auth_protocol: str = Field("sha", pattern="^(none|md5|sha|sha256)$")
    auth_key: str = ""
    priv_protocol: str = Field("aes", pattern="^(none|des|aes|aes256)$")
    priv_key: str = ""


class ScanIn(BaseModel):
    label: str = ""
    targets: list[str] = []
    broadcast: list[str] = []
    switches: list[str] = []
    snmp: Optional[SnmpIn] = None
    auto_switches: bool = True
    timeout: float = Field(2.0, ge=0.2, le=30)
    rate: float = Field(200.0, ge=1, le=2000)


@dataclass
class Job:
    id: str
    status: str = "running"  # running | done | failed
    log: list[str] = field(default_factory=list)
    scan_id: Optional[str] = None
    error: Optional[str] = None


def _is_loopback(host: str) -> bool:
    return client_allowed(host, [])


def _needs_admin(request: Request) -> bool:
    """Anything that sends traffic or changes data."""
    path = request.url.path
    if path in ("/api/login", "/api/logout", "/api/clientlog"):  # clientlog only writes to the log file
        return False
    return request.method not in ("GET", "HEAD") or path.startswith("/api/probe/")


def create_app(data_dir: Optional[str] = None, demo: bool = False, snmp_factory=None,
               allow: Optional[list] = None, demo_opcua: Optional[bool] = None,
               demo_opcua_port: int = DEMO_UA_PORT, collect: bool = True) -> FastAPI:
    """``allow``: client networks besides loopback that may connect. ``None`` disables the check (tests).

    ``collect``: read every dashboard in the background and record alarm history (``ghostmap/history.py``).

    ``demo_opcua`` (default: same as ``demo``) also starts the simulated OPC UA server for the Tag Browser.
    """
    if demo_opcua is None:
        demo_opcua = demo
    # sid -> [UaBrowser or None (closed while idle or after a drop), last_used, info, connect args]
    ua_sessions: dict[str, list] = {}
    demo_ua: dict = {}

    demo_ua_lock = asyncio.Lock()

    async def start_demo_ua() -> Optional[str]:
        """Start the simulated OPC UA gateway once; returns its endpoint, or None if it can't start."""
        async with demo_ua_lock:
            if "server" not in demo_ua:
                try:
                    from ghostmap.sim.opcua_server import SimUaServer

                    demo_ua["server"] = await SimUaServer(port=demo_opcua_port).start()
                except Exception:  # port taken or similar: everything else still works without it
                    demo_ua.pop("server", None)
                    return None
            return demo_ua["server"].endpoint

    async def resolve_ua_url(url: str) -> str:
        """Typing "demo" as the endpoint starts the simulated gateway on demand."""
        if url.strip().lower() != "demo":
            return url
        endpoint = await start_demo_ua()
        if endpoint is None:
            raise HTTPException(503, f"could not start the demo OPC UA server on port {demo_opcua_port}")
        return endpoint

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if demo_opcua:
            await start_demo_ua()
        if collect:
            collector.start()
        yield
        await collector.stop()
        for b, *_ in list(ua_sessions.values()):
            if b is not None:
                await b.disconnect()
        await live.close()
        history.close()
        if "server" in demo_ua:
            await demo_ua["server"].stop()

    app = FastAPI(title="Ghost Map", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    store = ScanStore(data_dir)
    log_path = debuglog.setup(store.root)
    started = time.time()
    log.info("Ghost Map %s starting; data in %s", __version__, store.root)
    users = UserStore(store.root)
    sessions = Sessions()
    lockout = Lockout()
    audit = AuditLog(store.root)
    jobs: dict[str, Job] = {}
    dashboards = DashboardStore(store.root)
    live = LiveValues(resolve_ua_url)
    history = History(store.root)
    collector = Collector(dashboards, live, history)
    tasks: set[asyncio.Task] = set()
    app.state.demo_ua, app.state.live, app.state.ua_sessions = demo_ua, live, ua_sessions  # for tests
    app.state.history, app.state.collector = history, collector

    def client_of(request: Request) -> str:
        return request.client.host if request.client else ""

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        client = client_of(request)
        if allow is not None and not client_allowed(client, allow):
            return PlainTextResponse("Forbidden", status_code=403, headers=SECURITY_HEADERS)
        if request.method not in ("GET", "HEAD") and not request.headers.get(CSRF_HEADER):
            return JSONResponse({"detail": "missing X-Ghostmap header"}, status_code=403, headers=SECURITY_HEADERS)
        session = sessions.get(request.cookies.get(SESSION_COOKIE))
        request.state.session = session
        path = request.url.path
        if users.has_users() and not path.startswith(PUBLIC_PATHS):
            if session is None:
                if path == "/":
                    return RedirectResponse("login", status_code=303, headers=SECURITY_HEADERS)
                return JSONResponse({"detail": "login required"}, status_code=401, headers=SECURITY_HEADERS)
            if _needs_admin(request) and session.role != "admin":
                return JSONResponse({"detail": "admin role required"}, status_code=403, headers=SECURITY_HEADERS)
        try:
            response = await call_next(request)
        except Exception as exc:  # anything not handled below: log the stack trace, answer with a reference
            ref = uuid.uuid4().hex[:8]
            log.exception("unhandled error %s on %s %s", ref, request.method, path)
            return JSONResponse({"detail": f"Internal error {ref} ({type(exc).__name__}). "
                                           "Details are in the debug log."}, status_code=500, headers=SECURITY_HEADERS)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    def who(request: Request) -> Optional[str]:
        s = request.state.session
        return s.user if s else None

    def load_demo() -> list[str]:
        from ghostmap.sim.machine import build_demo_scan

        ids = []
        for variant in ("baseline", "today"):
            scan = build_demo_scan(variant)
            store.save(scan)
            ids.append(scan.id)
        return ids

    if demo:
        load_demo()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/login")
    def login_page():
        return FileResponse(STATIC / "login.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.post("/api/login")
    def login(body: LoginIn, request: Request, response: Response):
        client = client_of(request)
        if lockout.locked(client, f"user:{body.user}"):
            audit.write(client, body.user, "login.locked")
            raise HTTPException(429, "too many failed logins; try again in a few minutes")
        role = users.verify(body.user, body.password)
        if role is None:
            lockout.fail(client, f"user:{body.user}")
            audit.write(client, body.user, "login.failed")
            raise HTTPException(401, "wrong user name or password")
        lockout.clear(client, f"user:{body.user}")
        token = sessions.create(body.user, role)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/")
        audit.write(client, body.user, "login.ok", role)
        return {"user": body.user, "role": role}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if sessions.get(token):
            audit.write(client_of(request), who(request), "logout")
        sessions.drop(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/info")
    def info(request: Request):
        s = request.state.session
        auth = users.has_users()
        return {"version": __version__, "data_dir": str(store.root), "auth": auth,
                "user": s.user if s else None, "role": s.role if s else ("admin" if not auth else None),
                "local": _is_loopback(client_of(request)),
                "demo_opcua": demo_ua["server"].endpoint if "server" in demo_ua else None}

    # ------------------------------------------------------------- users
    @app.post("/api/users/setup")
    def setup_first_admin(body: UserIn, request: Request, response: Response):
        """Create the first admin from the UI. Only from the machine itself, and only while no users exist."""
        client = client_of(request)
        if not _is_loopback(client):
            raise HTTPException(403, "the first login can only be created on the machine itself")
        if users.has_users():
            raise HTTPException(409, "logins are already set up")
        try:
            users.set(body.name, body.password, "admin")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        token = sessions.create(body.name, "admin")
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/")
        audit.write(client, body.name, "user.setup", "admin")
        return {"user": body.name, "role": "admin"}

    def require_admin(request: Request):
        s = request.state.session
        if users.has_users() and (s is None or s.role != "admin"):
            raise HTTPException(403, "admin role required")

    @app.get("/api/users")
    def list_users(request: Request):
        require_admin(request)
        return users.list()

    @app.post("/api/users")
    def add_user(body: UserIn, request: Request):
        require_admin(request)
        if not users.has_users():
            raise HTTPException(409, "create the first admin with setup")
        try:
            users.set(body.name, body.password, body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit.write(client_of(request), who(request), "user.set", f"{body.name} {body.role}")
        return users.list()

    @app.delete("/api/users/{name}")
    def remove_user(name: str, request: Request):
        require_admin(request)
        current = users.list()
        admins = [u["name"] for u in current if u["role"] == "admin"]
        if admins == [name]:
            raise HTTPException(409, "can't remove the last admin")
        try:
            users.remove(name)
        except KeyError:
            raise HTTPException(404, "no such user")
        audit.write(client_of(request), who(request), "user.remove", name)
        return users.list()

    # ------------------------------------------------------------- OPC UA tag browser (read-only)
    async def ua_open(args: dict):
        from ghostmap.collectors.opcua import UaBrowser

        try:
            browser = UaBrowser(await resolve_ua_url(args["url"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            info = await ua_call(browser.connect(args["security"], args["mode"], args["username"], args["password"],
                                                 pki_dir=store.root / "pki"))
        except HTTPException:
            await browser.disconnect()
            raise
        return browser, info

    ua_locks: dict[str, asyncio.Lock] = {}
    bg_tasks: set = set()  # keep a reference so a pending disconnect is not garbage-collected

    def ua_entry(sid: str) -> list:
        now = time.time()
        for k, entry in list(ua_sessions.items()):
            if now - entry[1] > UA_FORGET_S:
                ua_sessions.pop(k, None)
                ua_locks.pop(k, None)
            if now - entry[1] > UA_IDLE_S and entry[0] is not None:
                b, entry[0] = entry[0], None  # closed now, re-opened on next use
                log.info("opcua session %s idle, closed (re-opens on next use)", k[:8])
                task = asyncio.create_task(b.disconnect())
                bg_tasks.add(task)
                task.add_done_callback(bg_tasks.discard)
        entry = ua_sessions.get(sid)
        if entry is None:
            raise HTTPException(404, "OPC UA session closed; connect again")
        entry[1] = now
        return entry

    async def ua_get(sid: str):
        """The session's browser, re-opened if it was closed while idle or dropped."""
        entry = ua_entry(sid)
        if entry[0] is None:
            # Tree expands and the watch poll arrive together; only one of them may open the new session,
            # or the others' sessions are left open on the gateway (and FT Linx runs out of sessions).
            async with ua_locks.setdefault(sid, asyncio.Lock()):
                if entry[0] is None:
                    entry[0], _ = await ua_open(entry[3])
                    log.info("opcua session %s re-opened to %s", sid[:8], entry[0].url)
        return entry[0]

    async def ua_run(sid: str, fn):
        """Run ``fn(browser)`` on a Tag Browser session; if the connection dropped, re-open it and retry once."""
        from ghostmap.collectors.opcua import is_connection_error

        for attempt in (1, 2):
            browser = await ua_get(sid)
            try:
                return await asyncio.wait_for(fn(browser), timeout=60)
            except HTTPException:
                raise
            except Exception as exc:
                entry = ua_sessions.get(sid)
                if attempt == 1 and is_connection_error(exc) and entry is not None:
                    if entry[0] is browser:  # first request to notice the drop closes it; the others just retry
                        log.warning("opcua session %s to %s dropped (%s: %s); re-opening", sid[:8], browser.url,
                                    type(exc).__name__, exc)
                        entry[0] = None
                        await browser.disconnect()
                    continue
                log.warning("opcua request on %s failed: %s: %s", browser.url, type(exc).__name__, exc)
                raise HTTPException(502, f"{type(exc).__name__}: {exc}")

    async def ua_call(coro):
        try:
            return await asyncio.wait_for(coro, timeout=60)
        except HTTPException:
            raise
        except Exception as exc:  # asyncua raises its own error types (BadNodeIdUnknown, ...)
            log.warning("opcua call failed: %s: %s", type(exc).__name__, exc)
            raise HTTPException(502, f"{type(exc).__name__}: {exc}")

    @app.post("/api/opcua/endpoints")
    async def ua_endpoints(body: UaUrlIn, request: Request):
        from ghostmap.collectors.opcua import get_endpoints, normalize_endpoint

        try:
            url = normalize_endpoint(await resolve_ua_url(body.url))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit.write(client_of(request), who(request), "opcua.endpoints", url)
        return await ua_call(get_endpoints(url))

    @app.post("/api/opcua/connect")
    async def ua_connect(body: UaConnectIn, request: Request):
        if len(ua_sessions) >= UA_MAX_SESSIONS:
            oldest = min(ua_sessions, key=lambda k: ua_sessions[k][1])
            b = ua_sessions.pop(oldest)[0]
            if b is not None:
                await b.disconnect()
        args = {"url": body.url, "security": body.security, "mode": body.mode, "username": body.username,
                "password": body.password}
        browser, info = await ua_open(args)
        sid = uuid.uuid4().hex
        ua_sessions[sid] = [browser, time.time(), info, args]
        log.info("opcua session %s connected to %s (security %s)", sid[:8], browser.url, body.security)
        audit.write(client_of(request), who(request), "opcua.connect",
                    f"{browser.url} security={body.security} user={body.username or 'anonymous'}")
        return {"sid": sid, **info}

    @app.post("/api/opcua/disconnect")
    async def ua_disconnect(body: UaNodeIn):
        entry = ua_sessions.pop(body.sid, None)
        if entry and entry[0] is not None:
            await entry[0].disconnect()
        return {"ok": True}

    @app.post("/api/opcua/browse")
    async def ua_browse(body: UaNodeIn):
        return await ua_run(body.sid, lambda b: b.browse(body.node_id))

    @app.post("/api/opcua/attributes")
    async def ua_attributes(body: UaNodeIn):
        if not body.node_id:
            raise HTTPException(400, "node_id required")
        return await ua_run(body.sid, lambda b: b.attributes(body.node_id))

    @app.post("/api/opcua/read")
    async def ua_read(body: UaReadIn):
        return await ua_run(body.sid, lambda b: b.read(body.node_ids))

    ua_exports: dict[str, dict] = {}

    @app.post("/api/opcua/export")
    async def ua_export(body: UaNodeIn, request: Request):
        """Start a tag-list export in the background; poll GET /api/opcua/export/{job}.

        Whole controllers can take minutes, so this doesn't hold the HTTP request open.
        """
        browser = await ua_get(body.sid)
        audit.write(client_of(request), who(request), "opcua.export", f"{browser.url} {body.node_id or 'Objects'}")
        for k in [k for k, j in ua_exports.items() if j["status"] != "running"][:-4]:
            ua_exports.pop(k, None)  # keep only the last few finished exports in memory
        job = {"id": uuid.uuid4().hex[:12], "status": "running", "visited": 0, "tags": 0, "error": None, "result": None}
        ua_exports[job["id"]] = job

        def progress(visited, tags):
            job["visited"], job["tags"] = visited, tags
            entry = ua_sessions.get(body.sid)
            if entry:
                entry[1] = time.time()  # a long export counts as activity, so the session isn't dropped

        async def worker():
            try:
                job["result"] = await browser.export(body.node_id, progress=progress)
                job["status"] = "done"
            except Exception as exc:
                job["error"] = f"{type(exc).__name__}: {exc}"
                job["status"] = "failed"
                log.warning("tag export from %s failed after %d nodes: %s", browser.url, job["visited"], job["error"])

        task = asyncio.create_task(worker())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"job": job["id"]}

    @app.get("/api/opcua/export/{job_id}")
    def ua_export_status(job_id: str, request: Request):
        require_admin(request)
        job = ua_exports.get(job_id)
        if job is None:
            raise HTTPException(404, "export not found")
        return job

    # ------------------------------------------------------------- machine dashboards
    def load_dashboard(dash_id: str) -> dict:
        try:
            return dashboards.load(dash_id)
        except KeyError:
            raise HTTPException(404, "dashboard not found")

    @app.get("/api/dashboards")
    def list_dashboards():
        return dashboards.list()

    @app.post("/api/dashboards")
    def create_dashboard(body: DashboardIn, request: Request):
        """Build a dashboard from a tag export (rows with path, node_id, type, value)."""
        from ghostmap.analysis.dashboard import build_layout

        if body.job:  # the export already sits on the server: no need to send a whole controller back up
            job = ua_exports.get(body.job)
            if job is None or job["status"] != "done":
                raise HTTPException(404, "export not found or not finished")
            tags = [{"path": t["path"], "node_id": t["node_id"], "type": t.get("variant_type"), "value": t.get("value")}
                    for t in job["result"]["tags"]]
        else:
            tags = body.tags or []
        layout = build_layout(tags, name=body.name)
        if not layout["areas"]:
            raise HTTPException(400, "no usable tags in that export")
        dash = dashboards.create(body.name, body.endpoint.strip(), body.source, layout)
        dashboards.save_tags(dash["id"], tags)
        audit.write(client_of(request), who(request), "dashboard.create", f"{dash['id']} {body.endpoint}")
        return dash

    @app.get("/api/dashboards/{dash_id}")
    def get_dashboard(dash_id: str):
        return load_dashboard(dash_id)

    @app.post("/api/dashboards/{dash_id}/overrides")
    def set_overrides(dash_id: str, body: OverridesIn, request: Request):
        dash = load_dashboard(dash_id)
        dash["overrides"] = {"invert": {k: v for k, v in body.invert.items() if v}, "hidden": body.hidden}
        if body.name:
            dash["name"] = body.name
        dashboards.save(dash)
        audit.write(client_of(request), who(request), "dashboard.overrides", dash_id)
        return dash

    @app.post("/api/dashboards/{dash_id}/layout")
    def set_layout(dash_id: str, body: LayoutIn, request: Request):
        """Save a layout edited in the UI. Tags must come from the dashboard's own export."""
        from ghostmap.analysis.dashboard import clean_layout

        dash = load_dashboard(dash_id)
        catalog = dashboards.load_tags(dash_id)
        try:
            dash["layout"] = clean_layout(body.layout, {t[1] for t in catalog} if catalog else None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        dashboards.save(dash)
        audit.write(client_of(request), who(request), "dashboard.layout", dash_id)
        return dash

    @app.get("/api/dashboards/{dash_id}/tags")
    def dashboard_tags(dash_id: str, request: Request, q: str = "", limit: int = 200):
        """Search the tags discovered when the dashboard was built (for picking tags while editing)."""
        require_admin(request)
        load_dashboard(dash_id)
        squash = str.maketrans("", "", "_-. :/[]")  # "estop" finds E_Stop, "hpu temp" finds HPU_OverTemp
        words = [w.translate(squash) for w in q.lower().split()]
        out = []
        for path, nid, typ, value in dashboards.load_tags(dash_id):
            hay = f"{path} {nid}".lower().translate(squash)
            if all(w in hay for w in words):
                out.append({"path": path, "node_id": nid, "type": typ, "value": value})
                if len(out) >= min(max(limit, 1), 500):
                    break
        return out

    @app.get("/api/dashboards/{dash_id}/axis")
    async def axis_detail(dash_id: str, name: str = "", base: str = ""):
        """One axis in detail, read once on request: active fault/alarm/inhibit bits, status bits that are on,
        and motion, power, limit, tuning and fault-word values."""
        import re as _re

        from ghostmap.analysis.dashboard import AXIS_DETAIL, humanize

        dash = load_dashboard(dash_id)
        axis = next((x for a in dash["layout"]["areas"] for x in a.get("axes", []) if (x["base"] == base if base else x["name"] == name)), None)
        if axis is None:
            raise HTTPException(404, "no such axis")
        prefix = axis["base"] + "."
        members = {nid[len(prefix):]: (nid, typ) for _, nid, typ, _ in dashboards.load_tags(dash_id)
                   if nid.startswith(prefix) and "." not in nid[len(prefix):]}
        bits = [m for m, (_, typ) in members.items() if typ == "Boolean"
                and _re.search(r"(Fault|Alarm|Inhibit|Status)$", m)]
        wanted = [m for group in AXIS_DETAIL.values() for m in group if m in members]
        ids = [members[m][0] for m in bits + wanted][:1000]
        r = await live.read_once(dash, ids)
        if not r["ok"]:
            return r
        val = {m: r["values"].get(members[m][0]) for m in bits + wanted}
        kind = lambda m: "fault" if m.endswith("Fault") else "alarm" if m.endswith("Alarm") else "inhibit"  # noqa: E731
        checked = [m for m in bits if not m.endswith("Status")]
        return {"ok": True, "error": None, "checked": len(checked),
                "active": [{"node_id": members[m][0], "name": m, "label": humanize(m), "kind": kind(m)}
                           for m in checked if val[m] is True],
                "status_on": [humanize(m[:-len("Status")]) for m in bits if m.endswith("Status") and val[m] is True],
                "groups": {g: [{"name": m, "label": humanize(m), "value": val[m]} for m in ms if m in members]
                           for g, ms in AXIS_DETAIL.items() if any(m in members for m in ms)},
                "at": time.time()}

    @app.post("/api/dashboards/{dash_id}/rebuild")
    def rebuild_dashboard(dash_id: str, request: Request):
        """Lay the dashboard out again from its stored tag export with the current rules.

        Keeps the name, endpoint, running tags, heartbeat and comms settings; area edits are replaced.
        """
        from ghostmap.analysis.dashboard import build_layout

        dash = load_dashboard(dash_id)
        catalog = dashboards.load_tags(dash_id)
        if not catalog:
            raise HTTPException(409, "this dashboard has no stored tag export; build it again from the Tags tab")
        rows = [{"path": p, "node_id": n, "type": t, "value": v} for p, n, t, v in catalog]
        layout = build_layout(rows, name=dash["name"])
        known = {r["node_id"] for r in rows}
        old = dash["layout"].get("machine") or {}
        keep = {k: old[k] for k in ("mode", "freshness", "max_read_ms") if k in old}
        for k in ("running", "heartbeat"):
            if old.get(k) and all(n in known for n in old[k]):
                keep[k] = old[k]
        layout["machine"] = {**layout["machine"], **keep}
        dash["layout"] = layout
        ids = {a["id"] for a in layout["areas"]}
        ov = dash.get("overrides") or {}
        dash["overrides"] = {"invert": {k: v for k, v in (ov.get("invert") or {}).items() if k in ids},
                             "hidden": ov.get("hidden") or []}
        dashboards.save(dash)
        audit.write(client_of(request), who(request), "dashboard.rebuild", dash_id)
        return dash

    @app.delete("/api/dashboards/{dash_id}")
    async def delete_dashboard(dash_id: str, request: Request):
        load_dashboard(dash_id)
        dashboards.delete(dash_id)
        collector.forget(dash_id)
        history.forget(dash_id)
        await live.forget(dash_id)
        audit.write(client_of(request), who(request), "dashboard.delete", dash_id)
        return {"ok": True}

    @app.get("/api/dashboards/{dash_id}/values")
    async def dashboard_values(dash_id: str):
        """Current values (read-only OPC UA reads, cached for a second and shared by all viewers)."""
        from ghostmap.analysis.dashboard import node_ids

        dash = load_dashboard(dash_id)
        r = await live.read(dash, visible_node_ids(dash, node_ids(dash["layout"])))
        # The open stop's first-out alarm(s), so the page can mark which fault came first.
        return {**r, "first_out": history.current(dash_id)["first_out"], "recording": collector.status(dash_id)["recording"]}

    def history_window(hours: float) -> tuple[float, float]:
        if not 0 < hours <= RETENTION_DAYS * 24:
            raise HTTPException(400, f"hours must be between 0 and {RETENTION_DAYS * 24}")
        now = time.time()
        return now - hours * 3600, now

    @app.get("/api/dashboards/{dash_id}/history")
    def dashboard_history(dash_id: str, hours: float = 24, limit: int = 500):
        """Recorded stops (with first-out), alarm events and totals for the last ``hours``."""
        load_dashboard(dash_id)
        since, until = history_window(hours)
        limit = min(max(limit, 1), 5000)
        return {"since": since, "until": until, **collector.status(dash_id),
                "summary": history.summary(dash_id, since, until),
                "stops": history.stops(dash_id, since, until, limit=limit),
                "events": history.events(dash_id, since, until, limit=limit)}

    @app.get("/api/dashboards/{dash_id}/history.csv")
    def dashboard_history_csv(dash_id: str, hours: float = 24):
        dash = load_dashboard(dash_id)
        since, until = history_window(hours)
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", dash["name"]).strip("-") or "machine"
        return Response(history.events_csv(dash_id, since, until), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="ghostmap-alarms-{name}-{time.strftime("%Y%m%d")}.csv"'})

    @app.get("/api/dashboards/{dash_id}/health")
    def dashboard_health(dash_id: str):
        """Comms health and connection history of the dashboard's live reads (reads nothing)."""
        return live.health(load_dashboard(dash_id))

    @app.get("/api/dashboards/{dash_id}/verify")
    async def dashboard_verify(dash_id: str):
        """Read every alarm, running and heartbeat tag once and report the ones that can't be trusted."""
        from ghostmap.analysis.dashboard import verify_layout

        dash = load_dashboard(dash_id)
        L = dash["layout"]
        m = L.get("machine") or {}
        ids = list(dict.fromkeys([x["node_id"] for a in L["areas"] for x in a.get("alarms", [])]
                                 + m.get("running", []) + m.get("heartbeat", [])))
        r = await live.read_once(dash, ids, rows=True)
        if not r["ok"]:
            return {"ok": False, "error": r["error"]}
        out = verify_layout(L, dash.get("overrides", {}), r["rows"], live.history(dash))
        log.info("dashboard %s alarm check: %s", dash_id, {k: v for k, v in out["summary"].items() if k != "bad_by_plc"})
        return {"ok": True, "error": None, **out}

    # ------------------------------------------------------------- debug log
    client_log_seen: dict[str, list[float]] = {}

    @app.post("/api/clientlog")
    def client_log(body: ClientLogIn, request: Request):
        """Errors from the browser (uncaught exceptions on a page), written to the debug log."""
        client = client_of(request)
        now = time.time()
        recent = [t for t in client_log_seen.get(client, []) if now - t < 60]
        if len(recent) >= CLIENT_LOG_PER_MIN:
            return {"ok": False}
        client_log_seen[client] = recent + [now]
        log.warning("browser error on %s page from %s (%s): %s at %s:%d\n%s", body.page or "?", client,
                    who(request) or "-", body.message, body.source, body.line, body.stack)
        return {"ok": True}

    def debug_info() -> dict:
        return {"version": __version__, "data_dir": str(store.root), "log": str(log_path), "uptime_s": int(time.time() - started),
                "dashboards": [{k: d[k] for k in ("id", "name", "endpoint", "summary")} for d in dashboards.list()],
                "comms": live.snapshot(), "gateway_sessions": live.sessions(),
                "opcua_sessions": [{"url": e[3]["url"], "open": e[0] is not None, "idle_s": int(time.time() - e[1]),
                                    "security": e[3]["security"]} for e in ua_sessions.values()]}

    @app.get("/api/debug/log")
    def debug_log(request: Request, lines: int = 300):
        require_admin(request)
        return PlainTextResponse(debuglog.tail(store.root, min(max(lines, 1), 5000)))

    @app.get("/api/debug/bundle")
    def debug_bundle(request: Request):
        """Zip of the log files and versions/comms health, to send when reporting a problem. No tag values."""
        require_admin(request)
        audit.write(client_of(request), who(request), "debug.bundle")
        name = f"ghostmap-debug-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return Response(debuglog.bundle(store.root, debug_info()), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/scans")
    def list_scans():
        return store.list()

    @app.get("/api/scans/{scan_id}")
    def get_scan(scan_id: str):
        try:
            return to_dict(store.load(scan_id))
        except KeyError:
            raise HTTPException(404, "scan not found")

    @app.delete("/api/scans/{scan_id}")
    def delete_scan(scan_id: str, request: Request):
        try:
            store.delete(scan_id)
        except KeyError:
            raise HTTPException(404, "scan not found")
        audit.write(client_of(request), who(request), "scan.delete", scan_id)
        return {"ok": True}

    @app.get("/api/scans/{scan_id}/inventory.csv", response_class=PlainTextResponse)
    def export_csv(scan_id: str):
        try:
            scan = store.load(scan_id)
        except KeyError:
            raise HTTPException(404, "scan not found")
        return PlainTextResponse(inventory_csv(scan), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="ghostmap-{scan_id}.csv"'})

    @app.get("/api/diff")
    def diff(old: str, new: str):
        try:
            return diff_scans(store.load(old), store.load(new))
        except KeyError as exc:
            raise HTTPException(404, f"scan not found: {exc}")

    @app.post("/api/demo")
    def demo_load(request: Request):
        audit.write(client_of(request), who(request), "demo.load")
        return {"scans": load_demo()}

    @app.post("/api/scans")
    async def start_scan(body: ScanIn, request: Request):
        if not (body.targets or body.broadcast or body.switches):
            raise HTTPException(400, "nothing to scan")
        try:
            discovery.expand_targets(body.targets)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        snmp = None
        if body.snmp and (body.snmp.community or body.snmp.username):
            snmp = SnmpCredentials(**body.snmp.model_dump())
        req = ScanRequest(targets=body.targets, broadcast=body.broadcast, switches=body.switches, snmp=snmp,
                          auto_switches=body.auto_switches, timeout=body.timeout, rate=body.rate, label=body.label)
        job = Job(id=uuid.uuid4().hex[:12])
        jobs[job.id] = job
        audit.write(client_of(request), who(request), "scan.start",
                    f"job={job.id} targets={','.join(body.targets)} broadcast={','.join(body.broadcast)} "
                    f"switches={','.join(body.switches)}")

        async def worker():
            try:
                kwargs = {"snmp_factory": snmp_factory} if snmp_factory else {}
                scan = await run_scan(req, log=job.log.append, **kwargs)
                store.save(scan)
                job.scan_id = scan.id
                job.status = "done"
            except Exception as exc:  # surface to the UI
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "failed"

        task = asyncio.create_task(worker())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"job": job.id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job.__dict__

    @app.get("/api/probe/{ip}")
    async def probe(ip: str, request: Request):
        try:
            discovery.expand_targets([ip], max_hosts=1)
        except ValueError:
            raise HTTPException(400, "bad ip")
        audit.write(client_of(request), who(request), "probe", ip)
        ident = await discovery.probe(ip)
        port_list = (44818, 80, 443, 502, 102, 22, 23)
        open_ = await asyncio.gather(*(discovery.tcp_port_open(ip, p) for p in port_list))
        return {
            "ip": ip,
            "identity": to_dict(ident.identity) if ident else None,
            "tcp": dict(zip(map(str, port_list), open_)),
        }

    return app


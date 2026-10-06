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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ghostmap import __version__
from ghostmap.analysis.diff import diff_scans
from ghostmap.collectors import discovery
from ghostmap.models import to_dict
from ghostmap.protocols.snmp import SnmpCredentials
from ghostmap.scanner import ScanRequest, run_scan
from ghostmap.store import ScanStore, inventory_csv
from ghostmap.web.auth import AuditLog, Lockout, Sessions, UserStore, client_allowed

UA_IDLE_S = 600        # drop OPC UA sessions nobody has used for 10 minutes
UA_MAX_SESSIONS = 4
DEMO_UA_PORT = 4899    # not 4990, so the demo never collides with a real FT Linx Gateway on the HMI

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
    if path in ("/api/login", "/api/logout"):
        return False
    return request.method not in ("GET", "HEAD") or path.startswith("/api/probe/")


def create_app(data_dir: Optional[str] = None, demo: bool = False, snmp_factory=None,
               allow: Optional[list] = None, demo_opcua: Optional[bool] = None,
               demo_opcua_port: int = DEMO_UA_PORT) -> FastAPI:
    """``allow``: client networks besides loopback that may connect. ``None`` disables the check (tests).

    ``demo_opcua`` (default: same as ``demo``) also starts the simulated OPC UA server for the Tag Browser.
    """
    if demo_opcua is None:
        demo_opcua = demo
    ua_sessions: dict[str, list] = {}  # sid -> [UaBrowser, last_used, info]
    demo_ua: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if demo_opcua:
            try:
                from ghostmap.sim.opcua_server import SimUaServer

                demo_ua["server"] = await SimUaServer(port=demo_opcua_port).start()
            except Exception:  # port taken or similar: the demo still works without it
                demo_ua.pop("server", None)
        yield
        for b, _, _ in list(ua_sessions.values()):
            await b.disconnect()
        if "server" in demo_ua:
            await demo_ua["server"].stop()

    app = FastAPI(title="Ghost Map", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    store = ScanStore(data_dir)
    users = UserStore(store.root)
    sessions = Sessions()
    lockout = Lockout()
    audit = AuditLog(store.root)
    jobs: dict[str, Job] = {}
    tasks: set[asyncio.Task] = set()

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
        response = await call_next(request)
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
    def ua_get(sid: str):
        now = time.time()
        for k, (b, last, _) in list(ua_sessions.items()):
            if now - last > UA_IDLE_S:
                ua_sessions.pop(k, None)
                asyncio.create_task(b.disconnect())
        entry = ua_sessions.get(sid)
        if entry is None:
            raise HTTPException(404, "OPC UA session closed; connect again")
        entry[1] = now
        return entry[0]

    async def ua_call(coro):
        try:
            return await asyncio.wait_for(coro, timeout=60)
        except HTTPException:
            raise
        except (ValueError, asyncio.TimeoutError, OSError) as exc:
            raise HTTPException(502, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # asyncua raises its own error types (BadNodeIdUnknown, ...)
            raise HTTPException(502, f"{type(exc).__name__}: {exc}")

    @app.post("/api/opcua/endpoints")
    async def ua_endpoints(body: UaUrlIn, request: Request):
        from ghostmap.collectors.opcua import get_endpoints, normalize_endpoint

        try:
            url = normalize_endpoint(body.url)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit.write(client_of(request), who(request), "opcua.endpoints", url)
        return await ua_call(get_endpoints(url))

    @app.post("/api/opcua/connect")
    async def ua_connect(body: UaConnectIn, request: Request):
        from ghostmap.collectors.opcua import UaBrowser

        if len(ua_sessions) >= UA_MAX_SESSIONS:
            oldest = min(ua_sessions, key=lambda k: ua_sessions[k][1])
            await ua_sessions.pop(oldest)[0].disconnect()
        try:
            browser = UaBrowser(body.url)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        info = await ua_call(browser.connect(body.security, body.mode, body.username, body.password,
                                             pki_dir=store.root / "pki"))
        sid = uuid.uuid4().hex
        ua_sessions[sid] = [browser, time.time(), info]
        audit.write(client_of(request), who(request), "opcua.connect",
                    f"{browser.url} security={body.security} user={body.username or 'anonymous'}")
        return {"sid": sid, **info}

    @app.post("/api/opcua/disconnect")
    async def ua_disconnect(body: UaNodeIn):
        entry = ua_sessions.pop(body.sid, None)
        if entry:
            await entry[0].disconnect()
        return {"ok": True}

    @app.post("/api/opcua/browse")
    async def ua_browse(body: UaNodeIn):
        return await ua_call(ua_get(body.sid).browse(body.node_id))

    @app.post("/api/opcua/attributes")
    async def ua_attributes(body: UaNodeIn):
        if not body.node_id:
            raise HTTPException(400, "node_id required")
        return await ua_call(ua_get(body.sid).attributes(body.node_id))

    @app.post("/api/opcua/read")
    async def ua_read(body: UaReadIn):
        return await ua_call(ua_get(body.sid).read(body.node_ids))

    @app.post("/api/opcua/export")
    async def ua_export(body: UaNodeIn, request: Request):
        browser = ua_get(body.sid)
        audit.write(client_of(request), who(request), "opcua.export", f"{browser.url} {body.node_id or 'Objects'}")
        return await ua_call(browser.export(body.node_id))

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


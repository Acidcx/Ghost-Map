"""Local web UI / JSON API.

Bound to 127.0.0.1 by default. Static assets are served from the package, so
the UI works on air-gapped OT networks (no CDNs).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ghostmap import __version__
from ghostmap.analysis.diff import diff_scans
from ghostmap.collectors import discovery
from ghostmap.models import to_dict
from ghostmap.protocols.snmp import SnmpCredentials
from ghostmap.scanner import ScanRequest, run_scan
from ghostmap.store import ScanStore, inventory_csv

STATIC = Path(__file__).parent / "static"


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


def create_app(data_dir: Optional[str] = None, demo: bool = False, snmp_factory=None) -> FastAPI:
    app = FastAPI(title="Ghost Map", version=__version__)
    store = ScanStore(data_dir)
    jobs: dict[str, Job] = {}
    tasks: set[asyncio.Task] = set()

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

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/info")
    def info():
        return {"version": __version__, "data_dir": str(store.root)}

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
    def delete_scan(scan_id: str):
        try:
            store.delete(scan_id)
        except KeyError:
            raise HTTPException(404, "scan not found")
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
    def demo_load():
        return {"scans": load_demo()}

    @app.post("/api/scans")
    async def start_scan(body: ScanIn):
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
    async def probe(ip: str):
        try:
            discovery.expand_targets([ip], max_hosts=1)
        except ValueError:
            raise HTTPException(400, "bad ip")
        ident = await discovery.probe(ip)
        port_list = (44818, 80, 443, 502, 102, 22, 23)
        open_ = await asyncio.gather(*(discovery.tcp_port_open(ip, p) for p in port_list))
        return {
            "ip": ip,
            "identity": to_dict(ident.identity) if ident else None,
            "tcp": dict(zip(map(str, port_list), open_)),
        }

    return app


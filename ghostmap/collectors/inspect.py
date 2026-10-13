"""Device inspector: a close look at one IP address, read-only.

What it sends: TCP connects (opened and closed straight away, nothing written) to a fixed list of ports and,
for the latency samples, to one of them a few times; one EtherNet/IP ListIdentity and one ListServices
(the same unconnected, sessionless queries RSLinx browsing uses); and, only when a community is given,
SNMP GETs of the six system scalars. No CIP session, no I/O connection, no writes.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
from typing import Optional

from ghostmap.collectors import discovery
from ghostmap.models import to_dict
from ghostmap.protocols import mibs
from ghostmap.protocols.enip import ENIP_PORT, EnipError, build_list_services, parse_list_services

# (port, service, why it matters when open)
PORTS = [
    (44818, "EtherNet/IP (CIP)", ""),
    (4840, "OPC UA", ""),
    (4990, "FactoryTalk Linx Gateway (OPC UA)", ""),
    (502, "Modbus TCP", "Modbus has no login: anything that reaches it can write"),
    (102, "Siemens S7 / ISO-TSAP", ""),
    (80, "Web page (HTTP)", "Logins on plain HTTP cross the network unencrypted"),
    (443, "Web page (HTTPS)", ""),
    (22, "SSH", ""),
    (23, "Telnet", "Unencrypted remote login: turn it off if nobody uses it"),
    (21, "FTP", "Unencrypted file transfer and login"),
    (445, "Windows file sharing (SMB)", ""),
    (135, "Windows RPC", ""),
    (3389, "Remote Desktop", ""),
    (5900, "VNC remote screen", "VNC on HMIs often has no password"),
    (1433, "SQL Server", ""),
]
SAMPLES = 10
SAMPLE_GAP_S = 0.1


async def tcp_rtt(ip: str, port: int, timeout: float = 1.0) -> Optional[float]:
    """Time to complete a TCP connect, in ms (the connection is closed at once), or None."""
    t0 = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    ms = (time.perf_counter() - t0) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return round(ms, 2)


def latency_stats(samples: list) -> dict:
    ok = [s for s in samples if s is not None]
    out = {"samples": samples, "sent": len(samples), "lost": len(samples) - len(ok), "min": None, "avg": None,
           "max": None, "jitter": None}
    if ok:
        out.update(min=round(min(ok), 2), avg=round(statistics.fmean(ok), 2), max=round(max(ok), 2),
                   jitter=round(statistics.fmean(abs(a - b) for a, b in zip(ok, ok[1:])), 2) if len(ok) > 1 else 0.0)
    return out


async def list_services(ip: str, timeout: float = 1.5) -> Optional[list]:
    """EtherNet/IP ListServices over TCP 44818, or None when there is no answer."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, ENIP_PORT), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(build_list_services())
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(24), timeout)
        length = int.from_bytes(head[2:4], "little")
        body = await asyncio.wait_for(reader.readexactly(length), timeout) if length else b""
        return parse_list_services(head + body)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, EnipError):
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def snmp_system(ip: str, creds, factory) -> dict:
    client = factory(ip, creds)
    try:
        v = await client.get(list(mibs.SYSTEM_SCALARS))
    except Exception as exc:  # no answer, wrong community: say so, it's not an error of the inspector
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await client.close()
        except Exception:
            pass
    up = v.get(mibs.SYS_UPTIME)
    return {"ok": True, "name": str(v.get(mibs.SYS_NAME) or ""), "descr": str(v.get(mibs.SYS_DESCR) or ""),
            "location": str(v.get(mibs.SYS_LOCATION) or ""), "contact": str(v.get(mibs.SYS_CONTACT) or ""),
            "uptime_s": int(up) // 100 if isinstance(up, (int, float)) else None}


async def inspect(ip: str, snmp_creds=None, snmp_factory=None) -> dict:
    """Ports, identity, services, latency (and SNMP system info with a community) for one device."""
    ident_task = asyncio.create_task(discovery.probe(ip))
    services_task = asyncio.create_task(list_services(ip))
    opened = await asyncio.gather(*(discovery.tcp_port_open(ip, p) for p, _, _ in PORTS))
    ports = [{"port": p, "service": name, "open": ok, "note": note if ok else ""}
             for (p, name, note), ok in zip(PORTS, opened)]
    target = next((p["port"] for p in ports if p["open"]), None)
    samples = []
    if target is not None:
        for i in range(SAMPLES):
            samples.append(await tcp_rtt(ip, target))
            if i < SAMPLES - 1:
                await asyncio.sleep(SAMPLE_GAP_S)
    snmp = None
    if snmp_creds is not None:
        from ghostmap.protocols.snmp import PySnmpClient

        snmp = await snmp_system(ip, snmp_creds, snmp_factory or PySnmpClient)
    ident = await ident_task
    return {"ip": ip, "at": time.time(), "simulated": False, "ports": ports,
            "latency": {**latency_stats(samples), "port": target},
            "identity": to_dict(ident.identity) if ident else None, "services": await services_task, "snmp": snmp}


def simulated(ip: str, device: Optional[dict], port_info: Optional[dict]) -> dict:
    """What ``inspect`` would find on the demo machine (``ghostmap web --demo``): nothing is sent."""
    rng = random.Random(f"{ip}-{int(time.time() // 5)}")
    cip = bool(device and device.get("identity"))
    hmi = bool(device and "PanelView" in str((device.get("identity") or {}).get("product_name", "")))
    opened = {44818} if cip else set()
    if device and not cip:
        opened |= {80}  # a camera or PC: a web page, no CIP
    if hmi:
        opened |= {80, 5900}
    if device and (device.get("identity") or {}).get("device_type") == 0x0E:
        opened |= {80}
    reachable = bool(device and device.get("ip")) and (port_info or {}).get("oper_status", "up") == "up"
    base = 0.4 + rng.random() * 0.5
    bad = bool(port_info and (port_info.get("duplex") == "half" or port_info.get("fcs_errors")))
    samples = []
    for _ in range(SAMPLES):
        if not reachable:
            samples.append(None)
        elif bad and rng.random() < 0.2:
            samples.append(None)
        else:
            samples.append(round(base + (rng.random() * 9 if bad else rng.random() * 0.3), 2))
    ports = [{"port": p, "service": name, "open": reachable and p in opened, "note": note if reachable and p in opened else ""}
             for p, name, note in PORTS]
    return {"ip": ip, "at": time.time(), "simulated": True, "ports": ports,
            "latency": {**latency_stats(samples), "port": next((p["port"] for p in ports if p["open"]), None)},
            "identity": (device or {}).get("identity") if reachable else None,
            "services": [{"name": "Communications", "version": 1, "flags": 0x0120,
                          "capabilities": ["CIP over TCP (explicit messaging)", "CIP class 0/1 over UDP (I/O connections)"]}]
            if reachable and cip else None, "snmp": None}


def simulated_rtt(ip: str, port_info: Optional[dict]) -> Optional[float]:
    bad = bool(port_info and (port_info.get("duplex") == "half" or port_info.get("fcs_errors")))
    if port_info and port_info.get("oper_status") not in (None, "up"):
        return None
    if bad and random.random() < 0.15:
        return None
    return round(0.5 + random.random() * (8 if bad else 0.35), 2)

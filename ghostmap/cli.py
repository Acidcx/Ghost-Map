"""Ghost Map command line interface."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from typing import Any, Iterable, Optional, Sequence

from ghostmap import __version__
from ghostmap.models import ScanResult, to_dict
from ghostmap.protocols.enip import ENIP_PORT
from ghostmap.protocols.snmp import SnmpCredentials

_SEV_MARK = {"error": "[ERR ]", "warning": "[WARN]", "info": "[info]"}


# ---------------------------------------------------------------- output helpers

def table(rows: Iterable[Sequence[Any]], headers: Sequence[str]) -> str:
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    line = "  ".join("{:<%d}" % w for w in widths)
    out = [line.format(*headers), line.format(*["-" * w for w in widths])]
    out += [line.format(*r) for r in rows]
    return "\n".join(out)


def print_scan(scan: ScanResult) -> None:
    print(f"\nScan {scan.id}  {scan.params.get('label', '')}")
    for sw in scan.switches:
        print(f"\n== Switch {sw.sys_name or sw.ip} ({sw.ip})  {sw.model}  IOS {sw.software_version}  "
              f"S/N {sw.serial}  up {sw.uptime_seconds // 86400}d")
        for err in sw.errors:
            print(f"   ! could not read {err}")
        rows = [(p.name, p.alias, p.oper_status, f"{p.speed_mbps}M" if p.speed_mbps else "", p.duplex, p.vlan,
                 len(p.macs), p.fcs_errors + p.alignment_errors, "uplink" if p.is_uplink else "",
                 ", ".join(n.remote_name for n in p.neighbors))
                for p in sw.ports if p.is_physical]
        print(table(rows, ("port", "description", "link", "speed", "duplex", "vlan", "macs", "crc", "", "neighbor")))
    print("\n== Devices")
    rows = []
    for d in scan.devices:
        i = d.identity
        product = i.product_name if i else "(no EtherNet/IP)"
        if d.is_scanner:
            product += "  [this computer]"
        rows.append((d.ip or "-", d.mac or "", i.vendor_name if i else (f"{d.mac_vendor} (MAC)" if d.mac_vendor else ""), product,
                     i.revision if i else "", i.serial_hex if i else "", i.state_name if i else "",
                     f"{d.switch_name or d.switch_ip or ''} {d.switch_port or ''}".strip()))
    print(table(rows, ("ip", "mac", "vendor", "product", "fw", "serial", "state", "switch port")))
    print_findings(scan)


def print_findings(scan: ScanResult) -> None:
    print("\n== Findings")
    if not scan.findings:
        print("  none")
    for f in scan.findings:
        print(f"{_SEV_MARK.get(f.severity, f.severity)} {f.target}: {f.message}")
        if f.hint:
            print(f"        -> {f.hint}")


# ---------------------------------------------------------------- arguments

def _add_snmp_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("SNMP (read-only)")
    g.add_argument("--snmp-version", choices=("2c", "3"), default="2c")
    g.add_argument("--community", help="v2c community (or env GHOSTMAP_COMMUNITY)")
    g.add_argument("--v3-user")
    g.add_argument("--v3-auth", choices=("none", "md5", "sha", "sha256"), default="sha")
    g.add_argument("--v3-auth-key", help="or env GHOSTMAP_V3_AUTH_KEY; prompted if omitted")
    g.add_argument("--v3-priv", choices=("none", "des", "aes", "aes256"), default="aes")
    g.add_argument("--v3-priv-key", help="or env GHOSTMAP_V3_PRIV_KEY; prompted if omitted")
    g.add_argument("--snmp-timeout", type=float, default=2.0)
    g.add_argument("--snmp-port", type=int, default=161)


def _snmp_creds(args) -> Optional[SnmpCredentials]:
    if args.snmp_version == "3":
        if not args.v3_user:
            raise SystemExit("--v3-user is required for SNMPv3")
        auth = args.v3_auth_key or os.environ.get("GHOSTMAP_V3_AUTH_KEY")
        priv = args.v3_priv_key or os.environ.get("GHOSTMAP_V3_PRIV_KEY")
        if args.v3_auth != "none" and not auth:
            auth = getpass.getpass("SNMPv3 auth key: ")
        if args.v3_priv != "none" and not priv:
            priv = getpass.getpass("SNMPv3 priv key: ")
        return SnmpCredentials(version="3", username=args.v3_user, auth_protocol=args.v3_auth, auth_key=auth or "",
                               priv_protocol=args.v3_priv, priv_key=priv or "", timeout=args.snmp_timeout,
                               port=args.snmp_port)
    community = args.community or os.environ.get("GHOSTMAP_COMMUNITY")
    if not community:
        return None
    return SnmpCredentials(version="2c", community=community, timeout=args.snmp_timeout, port=args.snmp_port)


def _add_discovery_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("EtherNet/IP discovery")
    g.add_argument("targets", nargs="*", help="CIDRs, ranges (10.0.0.1-50) or IPs to sweep with ListIdentity")
    g.add_argument("-b", "--broadcast", action="append", default=[], help="broadcast address, e.g. 192.168.1.255")
    g.add_argument("--rate", type=float, default=200.0, help="packets/second (default 200)")
    g.add_argument("--timeout", type=float, default=2.0, help="seconds to wait for replies after the last send")
    g.add_argument("--enip-port", type=int, default=ENIP_PORT, help=argparse.SUPPRESS)


# ---------------------------------------------------------------- commands

def cmd_discover(args) -> int:
    from ghostmap.collectors.discovery import discover, expand_targets

    hosts = expand_targets(args.targets) if args.targets else []
    if not hosts and not args.broadcast:
        raise SystemExit("give at least one target or --broadcast address")
    replies, errors = asyncio.run(discover(hosts, args.broadcast, port=args.enip_port,
                                           timeout=args.timeout, rate=args.rate))
    if args.json:
        print(json.dumps(to_dict(replies), indent=2))
        return 0
    rows = sorted(((r.source_ip, r.identity.vendor_name, r.identity.device_type_name, r.identity.product_name,
                    r.identity.product_code, r.identity.revision, r.identity.serial_hex, r.identity.state_name,
                    r.identity.extended_status) for r in replies),
                  key=lambda r: tuple(int(x) for x in r[0].split(".")))
    print(table(rows, ("ip", "vendor", "type", "product", "code", "fw", "serial", "state", "status")))
    print(f"\n{len(rows)} device(s)")
    for e in errors:
        print(f"malformed reply: {e}", file=sys.stderr)
    return 0


def cmd_switch(args) -> int:
    from ghostmap.collectors.stratix import collect_switch
    from ghostmap.protocols.snmp import PySnmpClient

    creds = _snmp_creds(args)
    if creds is None:
        raise SystemExit("SNMP credentials required (--community or --snmp-version 3 --v3-user ...)")

    async def run():
        client = PySnmpClient(args.ip, creds)
        try:
            return await collect_switch(args.ip, client)
        finally:
            await client.close()

    sw = asyncio.run(run())
    if args.json:
        print(json.dumps(to_dict(sw), indent=2))
    else:
        print_scan(ScanResult(id="switch", started="", switches=[sw]))
    return 1 if any(e.startswith("system:") for e in sw.errors) else 0


def cmd_scan(args) -> int:
    from ghostmap.scanner import ScanRequest, run_scan
    from ghostmap.store import ScanStore

    req = ScanRequest(
        targets=args.targets, broadcast=args.broadcast, switches=args.switch, snmp=_snmp_creds(args),
        auto_switches=not args.no_auto_switches, timeout=args.timeout, rate=args.rate, enip_port=args.enip_port,
        baseline_path=args.baseline, label=args.label or "",
    )
    if not (req.targets or req.broadcast or req.switches):
        raise SystemExit("nothing to scan: give targets, --broadcast or --switch")
    scan = asyncio.run(run_scan(req, log=lambda m: print(m, file=sys.stderr)))
    if not args.no_save:
        path = ScanStore(args.data_dir).save(scan)
        print(f"saved {path}", file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(to_dict(scan), fh, indent=2)
    if args.csv:
        from ghostmap.store import inventory_csv
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            fh.write(inventory_csv(scan))
    if args.json:
        print(json.dumps(to_dict(scan), indent=2))
    else:
        print_scan(scan)
    return 2 if any(f.severity == "error" for f in scan.findings) else 0


def cmd_probe(args) -> int:
    from ghostmap.collectors.discovery import probe, tcp_port_open

    async def run():
        ident = await probe(args.ip, port=args.enip_port, timeout=args.timeout)
        ports = {p: await tcp_port_open(args.ip, p) for p in (44818, 80, 443, 502, 102, 22, 23)}
        return ident, ports

    ident, ports = asyncio.run(run())
    print(f"Probe {args.ip}")
    print("  TCP: " + "  ".join(f"{p}:{'open' if ok else '-'}" for p, ok in ports.items()))
    if ident is None:
        print("  No ListIdentity reply (not EtherNet/IP, filtered, or wrong IP/subnet).")
        return 1
    i = ident.identity
    for k, v in (("Vendor", f"{i.vendor_name} ({i.vendor_id})"), ("Type", f"{i.device_type_name} (0x{i.device_type:02X})"),
                 ("Product", f"{i.product_name} (code {i.product_code})"), ("Firmware", i.revision),
                 ("Serial", i.serial_hex), ("State", i.state_name), ("Status", f"0x{i.status:04X} {i.extended_status}"),
                 ("Flags", ", ".join(i.status_flags) or "-"), ("Reported IP", f"{i.socket_ip}:{i.socket_port}")):
        print(f"  {k:<12}{v}")
    return 0


def cmd_diff(args) -> int:
    from ghostmap.analysis.diff import diff_scans
    from ghostmap.store import ScanStore, load_scan_file

    def load(ref: str) -> ScanResult:
        return load_scan_file(ref) if ref.endswith(".json") else ScanStore(args.data_dir).load(ref)

    d = diff_scans(load(args.old), load(args.new))
    if args.json:
        print(json.dumps(d, indent=2))
        return 0
    print(f"Diff {d['old']} -> {d['new']}")
    for title, key in (("Added", "added"), ("Removed", "removed")):
        for x in d[key]:
            print(f"  {title:<8} {x['label']}  {x['mac'] or ''}")
    for x in d["replaced"]:
        print(f"  Replaced {x['label']}  serial {x['old_serial']} -> {x['new_serial']}, "
              f"fw {x['old_firmware']} -> {x['new_firmware']}")
    for x in d["changed"]:
        changes = "; ".join(f"{k}: {v['old']} -> {v['new']}" for k, v in x["changes"].items())
        print(f"  Changed  {x['label']}  {changes}")
    for f in d["new_findings"]:
        print(f"  + {f['severity']}: {f['target']}: {f['message']}")
    for f in d["resolved_findings"]:
        print(f"  - resolved: {f['target']}: {f['message']}")
    return 0


def cmd_web(args) -> int:
    import socket
    import threading
    import webbrowser

    import uvicorn

    from ghostmap.web.app import create_app

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    with socket.socket() as probe_sock:
        probe_sock.settimeout(0.5)
        already_running = probe_sock.connect_ex(("127.0.0.1", args.port)) == 0
    if already_running:
        print(f"Port {args.port} is already in use - Ghost Map is probably already running at {url}", file=sys.stderr)
        if args.open:
            webbrowser.open(url)
        return 0

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: serving on {args.host} - anyone who can reach this port can run scans.", file=sys.stderr)
    app = create_app(data_dir=args.data_dir, demo=args.demo)
    print(f"Ghost Map is running at {url}  (close this window or press Ctrl+C to stop)", file=sys.stderr)
    if args.open:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_simulate(args) -> int:
    from ghostmap.sim.machine import demo_machine, identity_reply
    from ghostmap.sim.responder import serve

    devices, _ = demo_machine(args.variant)
    prefix = args.prefix.rsplit(".", 1)[0]
    replies = {}
    for dev in devices:
        if dev.cip and dev.ip:
            sim_ip = f"{prefix}.{dev.ip.rsplit('.', 1)[1]}"
            sim_dev = type(dev)(sim_ip, dev.mac, dev.port, dev.cip)
            replies[sim_ip] = lambda ctx, d=sim_dev: identity_reply(d, ctx)

    async def run():
        await serve(replies, args.enip_port)
        print(f"Simulating {len(replies)} EtherNet/IP devices on {prefix}.0/24 UDP {args.enip_port}. Ctrl+C to stop.")
        print(f"Try: ghostmap discover {prefix}.0/24" + (f" --enip-port {args.enip_port}" if args.enip_port != ENIP_PORT else ""))
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ghostmap", description="Ghost Map - read-only OT device & Stratix switch mapper")
    p.add_argument("--version", action="version", version=f"ghostmap {__version__}")
    p.add_argument("--data-dir", default=None, help="scan storage (default ~/.ghostmap or $GHOSTMAP_DATA)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("discover", help="find EtherNet/IP devices (ListIdentity)")
    _add_discovery_args(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("switch", help="read a Stratix/managed switch over SNMP")
    s.add_argument("ip")
    _add_snmp_args(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_switch)

    s = sub.add_parser("scan", help="full scan: discovery + switches + correlation + diagnostics")
    _add_discovery_args(s)
    s.add_argument("-s", "--switch", action="append", default=[], help="switch IP to read (repeatable)")
    s.add_argument("--no-auto-switches", action="store_true", help="don't SNMP discovered CIP switches automatically")
    _add_snmp_args(s)
    s.add_argument("--baseline", help="firmware baseline JSON (see docs/baseline.example.json)")
    s.add_argument("--label", help="name for this scan, e.g. 'Line 1 FAT'")
    s.add_argument("--out", help="also write the full scan JSON here")
    s.add_argument("--csv", help="write device inventory CSV here")
    s.add_argument("--json", action="store_true", help="print JSON instead of tables")
    s.add_argument("--no-save", action="store_true", help="don't store in the scan history")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("probe", help="debug a single device: ListIdentity + common TCP ports")
    s.add_argument("ip")
    s.add_argument("--timeout", type=float, default=1.5)
    s.add_argument("--enip-port", type=int, default=ENIP_PORT, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("diff", help="compare two scans (ids from history, or .json files)")
    s.add_argument("old")
    s.add_argument("new")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("web", help="start the local web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8470)
    s.add_argument("--demo", action="store_true", help="preload the simulated demo machine scans")
    s.add_argument("--open", action="store_true", help="open the UI in the default browser")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("simulate", help="run a fake EtherNet/IP machine on loopback for bench testing")
    s.add_argument("--prefix", default="127.0.10.0", help="loopback /24 to bind devices on (Linux)")
    s.add_argument("--variant", choices=("today", "baseline"), default="today")
    s.add_argument("--enip-port", type=int, default=ENIP_PORT)
    s.set_defaults(func=cmd_simulate)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:  # double-clicked / no arguments: start the web UI and open the browser
        argv = ["web", "--open"]
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

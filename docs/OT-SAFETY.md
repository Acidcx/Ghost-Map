# OT safety notes

Ghost Map runs on live control networks, so it is deliberately conservative.

## What it sends

| Traffic | Purpose | Notes |
|---|---|---|
| EtherNet/IP `ListIdentity` (UDP 44818) | Discovery, identity, firmware, status | Unconnected and session-less. It is the same request RSLinx / FactoryTalk Linx browsing sends, and it opens no I/O or explicit connection. |
| SNMP GET / GETBULK (UDP 161) | Switch inventory, ports, MAC/ARP tables, neighbours | Read-only. The code has no `set` method. |
| TCP connect (probe only) | Port reachability check | Connects and closes immediately; no payload is sent. Runs only when you probe a single device. |
| OPC UA Browse / Read / GetEndpoints (TCP, default 4990 for FT Linx Gateway) | Tags (OPC UA) tab: browse the address space, read attributes and values, export tag lists | Only when an admin connects in the Tags tab. Watched tags are read every 1-5 s (or paused) and polling stops while the tab is hidden. The client code has no write, method-call or node-management calls, and a test checks that. Sessions close after 10 minutes idle. Server certificates are shown (SHA-1) but not yet checked against a trust list. A session idle for 10 minutes is closed, and it is re-opened with the same settings on next use or after a drop (one retry). |
| OPC UA Read (same server) | Machine dashboards: live values and alarm history | The server reads each saved dashboard's tags about once a second in the background (for the alarm history), whether or not anyone is watching; viewers share those reads. All dashboards on one gateway share one anonymous session. A dropped session is re-opened once, straight away; after a failed connect, reads wait 5 s before trying again. `ghostmap web --no-collect` turns background reading off, so a dashboard is read only while someone has it open. **Check all alarms** (Health page) reads the dashboard's alarm, running and heartbeat tags once, on request. The history is written to Ghost Map's own `history.db`; nothing is ever written to a PLC or gateway. |
| SQL Server SELECT (TCP, default 1433) | Production page: the TSC part schedule | Three fixed SELECTs on one view (`DataView.vPartScheduleCommon`): the lines, a line's unfinished parts, and its parts finished in the last 12 hours or this shift. The connection asks for a read-only session (`ApplicationIntent=ReadOnly`), and the code refuses to send anything that isn't a SELECT. Reads happen only while someone has the Production page open, and each answer is reused for 30 s. Use a SQL login that has only `SELECT` on the view's schema. The password is sealed with Windows DPAPI on disk and never returned by the API. |
| OS ARP | IP to MAC mapping | A side effect of contacting hosts on the local subnet. Ghost Map reads the OS ARP cache afterwards. |

It never sends CIP writes, OPC UA writes or method calls, SQL INSERT / UPDATE / DELETE / EXEC, resets, firmware updates, SNMP SET, or configuration changes.

## Load

- The discovery rate is limited (default **200 packets/s**, set with `--rate`). On networks with old or fragile devices, use `--rate 20`.
- Each target gets exactly one request; there are no retries.
- Ranges larger than 4096 hosts are refused unless the code limit is raised.
- SNMP walks are sequential per switch, use GETBULK with 25 repetitions, have a 2 s timeout, and retry once.

## Recommendations

- Get permission from the site or asset owner before scanning, and follow the site MOC process.
- Avoid scanning during critical operations (first runs, batch phases, safety-system testing).
- Prefer SNMPv3 with authPriv, plus a switch ACL that allows only the engineering laptop.
- The web UI binds to `127.0.0.1`. See **Web UI access** below before making it reachable from the network.

## Web UI access

Once the UI is reachable through the IXON IXrouter it is an outward-facing service on an OT asset, so:

- **Serving beyond localhost is refused** unless you pass `--allow` with the addresses that may connect (normally just the IXrouter's LAN IP) **and** at least one login exists. Example on an HMI:
  ```
  GhostMap.exe user add maint --role admin
  GhostMap.exe web --host 0.0.0.0 --allow 192.168.1.1
  ```
  Localhost (the HMI screen) can always connect. Every other address gets `403`.
- **First login.** The top bar shows **No login** while none exist. **Set up login** creates the first admin from the UI, but only for a browser on the machine itself.
- **Logins.** As soon as any user exists, every page and API call needs a login. Roles: `viewer` can look at everything; `admin` can also start scans, probe devices, load the demo and delete scans (anything that sends traffic or changes data). Manage users with `ghostmap user add|remove|list`. Passwords are stored as PBKDF2-SHA256 hashes in `users.json` in the data directory and need at least 10 characters.
- **Lockout.** 5 failed logins from one address or for one user name lock it out for 5 minutes.
- **Sessions** are random tokens in an HttpOnly, SameSite=Strict cookie, held in memory for 12 hours. Restarting Ghost Map logs everyone out.
- **Cross-site requests.** Anything that changes state must carry an `X-Ghostmap` header, which a page on another site can't add.
- **Headers.** A strict Content-Security-Policy (scripts only from Ghost Map itself), `nosniff`, no referrer, no caching. The API explorer (`/docs`) is turned off.
- **Audit log.** Logins (good, failed, locked out), logouts, scans started (with targets), probes, demo loads and deletions are appended to `audit.log` in the data directory, with time, client address and user. Passwords and SNMP credentials are never written.
- **Not yet covered:** HTTPS on the HMI itself. IXON encrypts the path from the user's browser to the IXrouter; the hop from the IXrouter to the HMI is plain HTTP on the machine network.
- Scan files contain the network inventory (IPs, MACs, serials, firmware). Treat them as sensitive site documentation.

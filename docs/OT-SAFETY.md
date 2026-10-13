# OT safety notes

Ghost Map runs on live control networks, so it is deliberately conservative.

## What it sends

| Traffic | Purpose | Notes |
|---|---|---|
| EtherNet/IP `ListIdentity` (UDP 44818) | Discovery, identity, firmware, status | Unconnected and session-less. It is the same request RSLinx / FactoryTalk Linx browsing sends, and it opens no I/O or explicit connection. |
| SNMP GET / GETBULK (UDP 161) | Switch inventory, CPU, memory, temperature, power, spanning tree, ports, traffic counters, MAC/ARP tables, neighbours | Read-only. The code has no `set` method. |
| TCP connect (Probe only) | Port reachability and response time | Connects and closes immediately; no payload is sent. Runs only when an admin inspects a single device: one connect to each of 15 common industrial and IT ports, then 10 timed connects (100 ms apart) to the first open one. **Live trace** times one connect a second to that port while it is on, and the server refuses more than four a second per browser. |
| EtherNet/IP `ListServices` (TCP 44818, Probe only) | Which CIP transports the device offers | One sessionless encapsulation request, like ListIdentity; no session is registered and no connection is opened. |
| SNMP GET of the six `system` scalars (UDP 161, Probe only) | Name, description, uptime of a non-switch device | Only when a community is typed into Probe. That community is used for that one request and is never saved or logged. |
| OPC UA Browse / Read / GetEndpoints (TCP, default 4990 for FT Linx Gateway) | Tags (OPC UA) tab: browse the address space, read attributes and values, export tag lists | Only when an admin connects in the Tags tab. Watched tags are read every 1-5 s (or paused) and polling stops while the tab is hidden. The client code has no write, method-call or node-management calls, and a test checks that. Sessions close after 10 minutes idle. Each server's certificate is pinned to its address the first time Ghost Map connects (`opcua-trust.json` in the data folder). If it changes, a signed or encrypted session is refused and a Security None session keeps reading with a warning, until an admin accepts the new certificate (Tags (OPC UA) > Gateway certificates, written to the audit log). A session idle for 10 minutes is closed, and it is re-opened with the same settings on next use or after a drop (one retry). |
| OPC UA Subscribe / Read (same server) | Machine dashboards: live values and alarm history | Each saved dashboard holds one OPC UA subscription on its tags, in the background (for the alarm history), whether or not anyone is watching; viewers share it. The gateway samples the tags every 250 ms (set per dashboard, 100 ms to 10 s) and sends only what changed, once a second, with up to 8 changes queued per tag. A subscription only asks the gateway to report values; it changes nothing. Every 5 s Ghost Map reads one server value (ServerStatus.State) to prove the session is alive, and every 60 s it reads the dashboard's tags in full to check nothing was missed. If the gateway refuses a subscription, or a dashboard is set to **Full read every second**, its tags are read in full once a second instead, and a refused subscription is asked for again after 10 minutes. All dashboards on one gateway share one anonymous session. A dropped session is re-opened once, straight away; after a failed connect, reads wait 5 s before trying again. `ghostmap web --no-collect` turns background reading off, so a dashboard is read only while someone has it open. **Check all alarms** (Health page) reads the dashboard's alarm, running and heartbeat tags once, on request. The history is written to Ghost Map's own `history.db`; nothing is ever written to a PLC or gateway. |
| SQL Server SELECT (TCP, default 1433) | Production page: the TSC part schedule | Fixed SELECTs on TSC's views: the lines and whole-order totals (`DataView.vPartScheduleCommon`), a line's queue (`DataView.vHmiPartScheduleQueueOrInProgress`) and its finished parts (`DataView.vPartScheduleCompleted`). When the login is allowed to, it also reads stations, station progress and dependencies, the shift calendar, stops (`dbo.vGetDownTimeData`), and the pattern, hole and notch tables and `Machine.ToolType` to count punch strokes; a read it isn't allowed is reported on the Connection page, not retried harder. It never reads `dbo.Order`, customer names or comments. The connection asks for a read-only session (`ApplicationIntent=ReadOnly`), and the code refuses to send anything that isn't a SELECT. Besides that, every 5 minutes one more SELECT per line sums the finished parts by hour since the last copy (the first time over the last 400 days) for Ghost Map's own production history (`production.db`); `ghostmap web --no-collect` turns it off. Other reads happen only while someone has the Production or Maintenance pages open; each answer is reused for 30 s, stations and the shift calendar for 10 minutes and the maintenance sums for 5 minutes (an older maintenance answer, up to 6 hours, is shown at once while a fresh one is read in the background, one at a time). The maintenance sums use one connection and one pass over each table, with a 120 s query timeout; any table that takes 5 s or more is written to the debug log and listed on the Connection page. Use a SQL login that has only `SELECT` on the view's schema. The password is sealed with Windows DPAPI on disk and never returned by the API. |
| Nothing (listening only) | Network > Live: passive listeners | Reads this PC's own ARP table every 10 s and listens on UDP 67 (BOOTP/DHCP requests), UDP 514 (syslog) and UDP 162 (SNMP traps). It never answers: it is not a BOOTP/DHCP server and does not acknowledge SNMP informs. Ports are bound without SO_REUSEADDR, so if another program (Rockwell BOOTP/DHCP Server, a DHCP server, another syslog or trap receiver) already has one, that listener stays off and says so. Switches send syslog and traps only if someone sets them to log to this PC; Ghost Map does not change switch settings. Trap communities are not stored. Runs with `ghostmap web` (and the service); `--no-listen` turns it off, and admins can switch each listener off on the page. Windows Firewall must allow inbound UDP for GhostMap.exe for anything but the ARP table to arrive. |
| OS ARP | IP to MAC mapping | A side effect of contacting hosts on the local subnet. Ghost Map reads the OS ARP cache afterwards. |
| Nothing (this PC only) | Maintenance > This PC and the alert list | Disk, memory, CPU and uptime from the OS every minute. Every 5 minutes one PowerShell call made only of `Get-CimInstance Win32_Service` (services whose name matches FactoryTalk, Rockwell, RSLinx, RSView or FT Linx) and `Get-WinEvent` (critical and error events in the System and Application logs over 24 h). It never starts, stops or changes a service. Alerts are written to this PC's own Application event log (source GhostMap); nothing is sent off the PC. |

It never sends CIP writes, OPC UA writes or method calls, SQL INSERT / UPDATE / DELETE / EXEC, resets, firmware updates, SNMP SET, or configuration changes.

## Load

- The discovery rate is limited (default **200 packets/s**, set with `--rate`). On networks with old or fragile devices, use `--rate 20`.
- Each target gets exactly one request; there are no retries.
- Ranges larger than 4096 hosts are refused unless the code limit is raised.
- SNMP walks are sequential per switch, use GETBULK with 25 repetitions, have a 2 s timeout, and retry once.
- A scan reads each switch's port counters twice, 10 s apart by default, for bandwidth and broadcast rates (about a dozen small walks each time; `--traffic-window 0` skips it).
- The traffic monitor (Network > Traffic), once an admin sets it up, reads the same counters plus CPU every poll interval (30 s by default, 10 s minimum) and every 10 minutes the port list and LLDP/CDP neighbours. Nothing else, and only from the switches listed.

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
  Localhost (the HMI screen) can always connect. Every other address gets `403`, and is written to the audit log (`access.refused`, once per address per 10 minutes) and shown on the alert list for an hour.
- **First login.** The top bar shows **No login** while none exist. **Set up login** creates the first admin from the UI, but only for a browser on the machine itself.
- **Logins.** As soon as any user exists, every page and API call needs a login. Roles: `viewer` can look at the machine, production, maintenance and network pages; `admin` can also start scans, probe devices, load the demo and delete scans (anything that sends traffic or changes data), and see users, connection settings, gateway certificates, tag lists, exports and the debug log. Manage users with `ghostmap user add|remove|list`. Passwords are stored as PBKDF2-SHA256 hashes in `users.json` in the data directory and need at least 10 characters.
- **Lockout.** 5 failed logins from one address or for one user name lock it out for 5 minutes.
- **Sessions** are random tokens in an HttpOnly, SameSite=Strict cookie (also Secure over HTTPS), held in memory for 12 hours. Restarting Ghost Map logs everyone out.
- **Cross-site requests.** Anything that changes state must carry an `X-Ghostmap` header, which a page on another site can't add.
- **Headers.** A strict Content-Security-Policy (scripts only from Ghost Map itself), `nosniff`, no referrer, no caching. The API explorer (`/docs`) is turned off.
- **Audit log.** Logins (good, failed, locked out), logouts, scans started (with targets), probes, demo loads and deletions are appended to `audit.log` in the data directory, with time, client address and user. Passwords and SNMP credentials are never written.
- **HTTPS** with `--https`: IXON encrypts the path from the user's browser to the IXrouter; HTTPS also encrypts the hop from the IXrouter to the HMI on the machine network. The certificate is self-signed (made on first start, private key readable only by the account Ghost Map runs as, in the data folder that the service limits to SYSTEM and Administrators) or the plant CA's (`--cert`/`--key`). Without `--https`, serving beyond localhost prints a reminder that logins cross the network unencrypted.
- **Tested on every route.** `tests/test_api.py` walks every API route and checks the rules above: state changes refused without the header, a login needed for everything but the login itself, viewers refused every change and the admin-only pages, the security headers on every answer, and saved passwords, SNMP communities and keys never returned.
- Scan files contain the network inventory (IPs, MACs, serials, firmware). Treat them as sensitive site documentation.

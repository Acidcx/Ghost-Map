# Ghost Map

Ghost Map is a read-only device mapper and commissioning/debug tool for OT networks. Plug a laptop into the machine network and it tells you:

- **What's on the wire.** It finds every EtherNet/IP device and reads its vendor, product, device type, firmware revision, serial number, and CIP state and status (owned, I/O faulted, major or minor fault).
- **Where each device is plugged in.** It reads Stratix and other Cisco IOS managed switches over SNMP: model, IOS version, serial, per-port link, speed, duplex, VLAN, CRC/alignment/late-collision counters, MAC table, ARP table, and LLDP/CDP neighbours. It then maps each device to its switch port.
- **What looks wrong.** It flags duplicate IPs, faulted devices, half duplex or a duplex mismatch, cabling/EMI error counters, firmware that is off-baseline or mixed, daisy-chained or unmanaged-switch ports, NAT'd devices, and switch data it couldn't read.
- **What changed since last time.** It compares two scans: devices added or removed, swapped hardware (same IP, new serial), firmware or state changes, and new or resolved findings.

It comes as a local web UI and a CLI. It works fully offline (no CDNs). Use the single-file `GhostMap.exe` on Windows, or run it from source with Python 3.10+ on Windows, Linux, or macOS.

> **Read-only by design.** Ghost Map only sends EtherNet/IP *ListIdentity* (the same unconnected query RSLinx/FactoryTalk Linx browsing uses), SNMP GET/GETBULK, and TCP connect checks. It has no write, set, or configuration code paths. See [docs/OT-SAFETY.md](docs/OT-SAFETY.md).

## Getting started on a laptop

There are three ways to run Ghost Map. Each one opens the web UI in your browser at `http://127.0.0.1:8470`.

### Option 1: GhostMap.exe (Windows, nothing to install)

1. On GitHub, open the **Actions** tab, click the latest green **Build Windows exe** run, and download **GhostMap-windows** under *Artifacts*. Tagged versions (`v0.1.0`, ...) also attach `GhostMap.exe` to a **Release**.
2. Unzip it and double-click `GhostMap.exe`.
   - Windows SmartScreen may say "Windows protected your PC" because the exe isn't code-signed yet. Click **More info**, then **Run anyway**.
   - If Windows Firewall asks, allow it on **Private** networks.
3. A console window opens and your browser shows Ghost Map. **Closing the console window stops Ghost Map.**

### On the machine's HMI: as a Windows service

So Ghost Map runs from boot, records history whether or not anyone is logged on, and comes back by itself after a crash, install it as a service. From a Command Prompt opened with **Run as administrator**, in the folder with the downloaded exe:

```bat
GhostMap.exe service install
```

This copies the exe to `C:\Program Files\GhostMap`, copies your existing `~/.ghostmap` (dashboards, history, settings, logins) to `C:\ProgramData\GhostMap` the first time, and limits that folder to SYSTEM and Administrators. It registers the service **Ghost Map** (LocalSystem, starts with Windows after a short delay, restarted 10 s, 30 s and 60 s after a failure) and starts it. Open `http://127.0.0.1:8470` as before; double-clicking `GhostMap.exe` just opens the browser while the service runs. To upgrade, run `service install` again from the new exe. `service status`, `service stop`, `service start` and `service uninstall` (keeps the data) do what they say. To take Ghost Map off a PC for good, `service uninstall --purge` also deletes the data folders (logins, saved passwords, certificates, history), the event log source and the firewall rules for the installed exe; then delete the exe you ran it from. For IXON access, install it first as above, add a login (**Set up login** on the HMI, or `GhostMap.exe user add maint --role admin` from an elevated prompt), then run `service install` again with the same options as `web`, for example `GhostMap.exe service install --host 0.0.0.0 --allow 192.168.1.1`. In that order the first install still copies your existing data. Windows shows no firewall prompt for a service, so also add an inbound rule for TCP 8470 from the allowed addresses, and check that the HMI's default gateway is the IXrouter (or that the IXrouter NATs) so replies find their way back. Once installed, command-line tools run as an administrator use `C:\ProgramData\GhostMap` too.

### Option 2: Start-GhostMap.bat (Windows, from the source code)

Needs [Python 3.10+](https://www.python.org/downloads/). When installing Python, tick **Add python.exe to PATH**.

1. On GitHub, use **Code > Download ZIP** (or `git clone`) and unzip it.
2. Double-click **`Start-GhostMap.bat`**. The first run sets everything up (needs internet once, takes about a minute). After that it starts straight away.

On macOS or Linux, run `./start-ghostmap.sh` instead.

### Option 3: Command line

```bash
pip install -e .
ghostmap            # same as: ghostmap web --open
```

## Your first scan

1. **Try the demo first.** On the start screen click **Load demo machine** (or run `GhostMap.exe web --demo`). It loads a simulated packaging line with problems already planted, so you can see what every tab does.
2. **Connect the laptop to the machine network.** Plug into a spare switch port. Give the laptop a static IP in the machine subnet that nobody else uses (e.g. `192.168.1.250 / 255.255.255.0`).
3. **Click New scan** and fill in:
   - **Targets**: the machine subnet, e.g. `192.168.1.0/24`.
   - **Switch IPs** (optional): your Stratix, e.g. `192.168.1.2`. Any Stratix found by discovery is added automatically.
   - **SNMP**: the switch's read-only community (v2c) or v3 user and keys. Without SNMP you still get the device list, firmware and faults, but no switch ports. See [Preparing a Stratix switch](#preparing-a-stratix-switch).
4. **Start scan.** A /24 takes about 5 seconds. The results show up in these tabs:
   - **Overview**: what's wrong, with a hint for each problem.
   - **Devices**: the full inventory.
   - **Switches**: port faceplate and counters.
   - **Compare**: against an earlier scan (e.g. the as-commissioned scan).
5. **Export CSV** gives you the device list for the machine documentation.

What you'll see in the device list:
- **EtherNet/IP devices** with product, firmware, serial and status.
- **PCs running RSLinx Classic or FactoryTalk Linx.** They answer too, as vendor *Rockwell Software*, type *Workstation*, with the PC's hostname as the product name. Your own laptop is tagged **this computer**.
- **Everything else on the subnet that answered**, such as Moxa, Siemens and IT switches, cameras and PCs. These are listed with the manufacturer taken from the MAC address. They get no firmware or serial, because they don't speak EtherNet/IP.

A device on a different subnet won't be seen. For example, Moxa switches ship with `192.168.127.253`. Give the laptop a second IP in that subnet and add it to Targets.

If discovery finds nothing:
- Check the laptop's IP and subnet mask.
- Check that Windows Firewall allowed Ghost Map.
- Try **Probe** on one known device IP.

## Command line reference

Everything in the UI is also available from the command line. With the exe, use `GhostMap.exe <command>` from a Command Prompt in the folder where the exe is.

```bash
# Who's out there? (EtherNet/IP sweep, 200 packets/s by default)
ghostmap discover 192.168.1.0/24

# Read one Stratix switch
ghostmap switch 192.168.1.2 --community <ro-community>
ghostmap switch 192.168.1.2 --snmp-version 3 --v3-user ghostmap      # prompts for keys

# Full scan: discovery + switches + correlation + diagnostics, saved to history
ghostmap scan 192.168.1.0/24 --switch 192.168.1.2 --community <ro> \
    --baseline docs/baseline.example.json --label "Line 1 FAT" --csv line1.csv

# Debug a single device (ListIdentity + TCP 44818/80/443/502/102/22/23)
ghostmap probe 192.168.1.30

# What changed since commissioning?
ghostmap diff <old-scan-id> <new-scan-id>      # ids from the web UI / ~/.ghostmap/scans
```

When SNMP credentials are given, any discovered device that identifies as a CIP *Managed Ethernet Switch* (Stratix answers ListIdentity) is read automatically. Use `--no-auto-switches` to turn that off.

`scan` exits with code `2` if any error-level finding was raised, so it can gate a FAT/SAT script.

### Bench testing without hardware

```bash
ghostmap simulate                     # 9 fake EtherNet/IP devices on 127.0.10.0/24 (Linux)
ghostmap discover 127.0.10.0/24
```

## Web UI

`ghostmap web` serves on `127.0.0.1:8470` by default. Its pages are grouped by where their data comes from:

- **Machine** (OPC UA, from the PLCs through FactoryTalk Linx Gateway)
  - **Dashboard**: a dashboard for the machine, laid out automatically from its PLC tags (see below). Viewers can watch it; admins build and edit it.
  - **Tags**: a read-only tag browser in the style of UaExpert, for FactoryTalk Linx Gateway or any OPC UA server. Type an endpoint (a bare IP works; FT Linx Gateway's default port 4990 is added), optionally pick a security policy and login, and connect. Browse the address space, see a node's attributes, double-click tags to watch them live, and **Export tags** to get every tag under a node as CSV, which is handy for comparing naming between machines. Type `demo` as the endpoint (or start with `ghostmap web --demo`) to connect to a simulated gateway with two presses whose tag names drift. The first certificate each server shows is pinned (**Gateway certificates**, under the browser): if it changes later, a signed or encrypted session is refused, and a Security None one keeps reading with a warning, until an admin accepts the new one.
- **Production** (SQL, from the TSC database)
  - **Line**: the line's part schedule (see below).
  - **Connection**: the TSC database settings, what the SQL login can read, and moving dashboards and settings to another PC.
- **Maintenance** (counters from TSC and the PLCs)
  - **Service**: service items with an interval (hole hits, pieces, feet, hours or days), due-soon warnings and a log of the work done.
  - **Counters**: all-time totals as odometers, then feet, pieces, production hours and hole hits per tool from TSC, and run hours per machine from the PLC. A big TSC database can take a while the first time; after that the last answer is shown at once and refreshed in the background, and the Connection page lists the slowest tables.
  - **This PC**: the health of the PC Ghost Map runs on (the HMI). Disk space, memory, CPU and uptime every minute; every 5 minutes the FactoryTalk and Rockwell services (one that is set to start automatically but isn't running is flagged) and the errors in the Windows System and Application logs over the last 24 hours, grouped by source. Ghost Map only looks: it never starts or stops a service.
- **Network** (TCP/IP: EtherNet/IP and SNMP scans)
  - **Live**: what Ghost Map hears without sending anything, refreshed every 2 s. This PC's ARP table (new devices, devices not in the last scan, an IP that suddenly answers from another MAC), modules asking for an IP over BOOTP/DHCP (a reset or replaced module, never answered), and the syslog and SNMP traps switches send when they are set to log to this PC (ports down/up, err-disabled ports, loops, duplex mismatches, restarts). Each event has a hint, each listener shows whether it is listening, and an admin can switch each one off. `ghostmap web --no-listen` turns them all off. In the demo the traffic is simulated.
  - **Overview**: counts and all findings, with a hint for each, and a one-line summary of what Live heard in the last hour.
  - **Devices**: a sortable, filterable inventory. Click a device for its full CIP identity, decoded status word, related findings, and a one-click probe.
  - **Switches**: a port faceplate coloured by health, a port table, and per-port detail (counters, neighbours, MACs mapped to devices).
  - **Compare**: the diff of any two scans.
  - **Probe**: a close look at one device. A health score and a plain-language verdict with hints; response time from ten timed TCP connects (min, average, max, jitter, lost) and a **Live trace** of one connect a second; which of 15 industrial and IT ports are open (Modbus, Telnet, VNC and plain HTTP are flagged); CIP identity and the transports from ListServices; where it is plugged in (switch, port, speed, duplex, errors, with a button to inspect the switch); and what earlier scans saw at that address (a replaced module, a firmware change, a moved cable). Optional SNMP system info with a community you type in, which is not saved. In the demo, the simulated machine answers without sending anything: try 192.168.1.20 (a drive on a half-duplex port with errors) or 192.168.1.40 (an HMI with VNC open).

**Export CSV** downloads the device inventory.

### Alerts

**Alerts** in the top bar counts everything that needs attention, from every page: this PC (a disk over 90% full, memory over 90%, CPU busy for 5 minutes, a FactoryTalk service stopped, a burst of event-log errors), machine comms (no data from the gateway for a minute, a frozen heartbeat, unreadable tags, a changed gateway certificate), a machine stopped for 10 minutes or more with its first-out alarm, switch problems from the traffic monitor and switches not answering SNMP, new devices and IP conflicts heard on Live in the last day, overdue service items, and the TSC database not answering. Click it for the list: each alert has what to do and a link to the page with more. Errors also show as a red line under the tabs on every page. Each alert is written to Ghost Map's log and, on Windows, to the Application event log (source **GhostMap**, event ID 100 for this PC, 200 comms, 300 machine, 400 network, 500 maintenance, 600 production, one higher when it clears), so Event Viewer or a monitoring agent can pick them up. The list is checked every 30 s.

### Machine dashboards

In **Tags (OPC UA)**, connect, select the controller (or just its fault folder) and press **Build dashboard**. Ghost Map reads every tag under it and lays out a dashboard: one card per folder (area) with its alarms, worst first, plus timers, fault words, counters and run state. Or press **From CSV...** on the Machine tab and pick an **Export tags** file. Tag names drift between machines, so the layout leans on structure first (TIMER and COUNTER members, data types, the folder a tag sits in) and on names second (`_Warn`, `E_Stop`, `_MS`, `Comms_Flt`, `OverTemp`...). Where it isn't sure it says so: for example, an area whose bits are mostly on when exported may use "on = healthy" (comms OK bits). Motion axes (AXIS_CIP_DRIVE, about 600 members each) collapse into one item showing state, enable/homed, position, velocity and fault words; **Which faults?** reads the axis's fault, alarm and inhibit bits once, on request. To keep live reads light, I/O module tags, arrays outside fault folders and long lists of status bits are left out (the dashboard says how many). Tick **Edit** to flip an area, rename or remove items and areas, change the tag behind an item, or add tags. Tags are picked from the ones found when the dashboard was built, so nothing is typed in by hand. Long names are cut short with "..."; hover for the full name and the raw tag. **Rebuild layout** (in Edit) lays an older dashboard out again with the current rules from the tags stored when it was built, keeping its running tags, heartbeat and flipped areas.

A whole controller is laid out in three levels: **Overview** (the tiles, everything active right now, and one card per PLC program), **Drives** (every motion axis in one table, grouped by program, faulted first; **Details** opens an axis's active faults, alarms and inhibits, the status bits that are on, and motion, power, limit, tuning and fault-word values, refreshed while open), and one page per program (per controller and program when the dashboard spans several PLCs) with its area cards grouped by top folder, a filter, an "only areas with something active" switch, and a compact Signals panel. One-shot storage (`OSg`, `OS1`, `ONS`), command bits in fault folders (`Reset_Faults`) are left out, and bits like `No_Faults` count as active when off. FT Linx Gateway lists a program's InOut parameters and aliases as tags of their own, so the same UDT can appear a dozen times; copies of controller-scope tags are dropped (same members, types and BOOL values). Add-On Instruction EnableIn/EnableOut, MSG and motion instruction tags, strings and status bits with no recognisable job are left out too, and stay searchable in Add tag. The **Machine** tile reads the running tag Ghost Map guessed (it prefers `Running` or `RunF` over names like `AutoBatchRunout`); in Edit, its Edit button lets you add or remove running tags and choose whether any or all of them must be on. Live values come from one OPC UA subscription per dashboard, held by the Ghost Map service and shared by everyone watching, over one session per gateway however many dashboards use it: the gateway samples the tags every 250 ms and sends only what changed. If the gateway refuses a subscription, or you set **Values by** to **Full read every second** (Health page, in Edit), the tags are read in full once a second instead. Dashboards are saved in `~/.ghostmap/dashboards`.

**Alarm history and first-out.** While Ghost Map runs, it follows every saved dashboard in the background, whether or not anyone has a page open, and records each alarm (and faulted motion axis) from when it went active until it cleared. A **stop** runs from the first alarm after a clear machine until every alarm has cleared; the alarm that started it is its **first out**, marked on the Overview while the stop is on. With a subscription, every change the gateway reports goes into the history at the time it happened, so an alarm that is on for a fraction of a second is still recorded, and alarms a quarter of a second apart get the right first-out; only alarms that go active in the same 250 ms sample are a tie. The history runs about 2 seconds behind the machine so late notifications land in order. With full reads once a second, alarms that go active within the same second are shown as a tie. A bit that is on for only one PLC scan can still be missed either way: count it in the PLC instead. The **History** page lists the stops with their first-out, the alarms that are first-out, active or active longest most often, the alarm log, and comms gaps (reads that failed: alarm states are held through a gap, never guessed), over the last 8 hours to 90 days, with **Download CSV**. History is a SQLite file, `~/.ghostmap/history.db`, kept for 90 days; open it with DB Browser for SQLite if you want to query it yourself. Ghost Map writes only to that file, never to a PLC or gateway. `ghostmap web --no-collect` turns background reading and history off.

**Can the dashboard be trusted?** The **Comms** tile and the **Health** page show how many of the dashboard's tags read Good (per PLC, with the status the gateway returned for the rest), read time, drops and reconnects, and a heartbeat: a tag the PLC changes all the time (Ghost Map picks one named like `Heartbeat` or `Watchdog`; choose your own in Edit, preferably a counter). If the heartbeat stops changing, the data may be frozen even though every tag reads Good, so faults show "?" instead of OK. If there's no reliable heartbeat, set **Freshness check** to **Response time only** (Health page, in Edit): data then counts as fresh while every read comes back within the limit you set (default 2000 ms). A tag that can't be read shows grey, never OK. **Check all alarms** reads every alarm once and lists the ones that can't be trusted: tags the gateway doesn't know (renamed in the PLC), tags it can't read, alarms that aren't BOOLs, duplicates, and areas where most bits are on.

**Debug log.** Ghost Map writes its own log to `~/.ghostmap/logs/ghostmap.log`: server errors with stack traces, OPC UA drops and reconnects, and uncaught errors from the browser pages. Admins open it with **Debug log** in the top bar, and **Download debug bundle** zips it with versions and comms health (no tag values, but it does contain gateway addresses and user names).

Scans are stored as JSON in `~/.ghostmap/scans` (override with `--data-dir` or `GHOSTMAP_DATA`). SNMP credentials are never written to disk.

### Production (TSC part schedule)

**Production > Line** shows one line from the TSC part schedule. TSC's words: an **order** holds jobs, a job holds **bundles**, a bundle holds **parts** (one row of the cut list: a profile, a length and a quantity), and each part is made as **pieces**. Statuses are TSC's own (`dbo.PartStatus`: Imported, Pending, Queued, Hold, IP, Completed, Error). The page shows, for this shift: orders worked, pieces made (good and scrap) and feet made, feet per hour averaged over the shift, running time (the share of the shift with no TSC stop open; planned stops such as breaks count as stops, so it is not availability) and stops with TSC's stop reasons, scrap pieces and remakes; one card per **station** (a separate machine on the line, such as a punch, a notcher, or the pan and back skin rollformers that feed a sandwich press, which waits for both); the part running now with its progress at each station and a finish estimate; parts on hold; feet per clock hour for the last 12 hours; the orders on the schedule with bundles, pieces done of the whole order and feet left; the queue in TSC's run order; and the last 15 parts finished. Thickness is shown in inches, as TSC stores it. It refreshes every 15 seconds; the server reuses each read for 30 seconds, so many people watching cost one query.

**History**, at the bottom of the page, shows parts, pieces, feet, scrap pieces and production hours per day (last 14) or per week (last 12), with a CSV download. Ghost Map keeps these numbers itself, in `production.db` in its data folder: every 5 minutes it copies each line's finished parts from TSC, summed by the hour they finished in, so the history stays when TSC clears old parts or the SQL Server can't be reached. The first copy goes back 400 days. Days start at the first shift's start (from TSC's shift calendar, else the settings) so a night shift isn't split at midnight; weeks start on Monday. Pointing Ghost Map at another TSC database starts the history again.

It reads TSC's own views with fixed SELECTs on a read-only connection (`ApplicationIntent=ReadOnly`): `DataView.vHmiPartScheduleQueueOrInProgress` for the queue (status 2 queued, 3 on hold, 4 in progress, ordered by `QueueIndex`), `DataView.vPartScheduleCompleted` for finished parts (status 5) and `DataView.vPartScheduleCommon` for the lines and whole-order totals. Imported and pending parts (status 0 and 1) aren't in the queue until TSC queues them. TSC's `1900-01-01` means "no time yet". Customer names and comments are never read, and Ghost Map never reads `dbo.Order`.

With a few more read grants it also shows stations (`dbo.Station`, `dbo.StationPart`, `dbo.StationDependency`), the shift from TSC's calendar (`dbo.Shift`, `dbo.DayOfWeek`; otherwise the shift start times in the settings), stops and reasons (`dbo.vGetDownTimeData`) and hole hits (`dbo.PartPattern`, `dbo.PatternHole`, `dbo.Hole`, `dbo.PartNotch`, `Machine.ToolType`). Without them the page still works; **What Ghost Map can read** on the Connection page lists what each one adds and the exact GRANT to run. None of these tables hold customer data.

Set it up under **Production > Connection** (admin):

- **Server**: the SQL Server's name or IP. Add the instance (`SQLHOST\TSC`) or the port (`10.0.0.5,1433`) if it isn't the default instance on 1433.
- **Database**: the TSC database's name.
- **SQL login** and **password**: a SQL Server login (not a Windows account) that can only read the view's schema, for example:
  ```sql
  CREATE LOGIN ghostmap_ro WITH PASSWORD = '...';
  USE <TSC database>;
  CREATE USER ghostmap_ro FOR LOGIN ghostmap_ro;
  GRANT SELECT ON SCHEMA::DataView TO ghostmap_ro;
  ```
- **Encrypt the connection** if the server requires it; **Trust the server's certificate** accepts its self-signed certificate (like the SSMS option), or untick it and give the CA file.
- Under **More settings**: the three views, the unit of the Length column (inches by default), the shift start times (default `06:00,18:00`) and how long a read is reused.

**Test connection** connects and lists the lines without saving. The password is saved sealed with Windows DPAPI for this PC (`~/.ghostmap/tsc.json`) and is never sent back to a browser, written to the log or included in an export. GhostMap.exe brings its own SQL driver (python-tds), so nothing needs installing on the HMI. Type `demo` as the server to try a simulated schedule.

### Maintenance

**Maintenance > Service** lists service items, overdue first. Each one counts up from when it was last done, against an interval:

- **Hole hits** at a station, for one tool type or all of them: pieces made times the holes and notches on each part (TSC's patterns, holes and notches; a repeat pattern counts once per repeat along the part's length). This is an estimate of punch wear, not press strokes: a gang die that punches four holes in one stroke counts four hits, so set die-life intervals in hits.
- **Pieces made** (each piece is a shear cut), **feet made**, or **production hours** (start to end of each part) on a TSC line.
- **Run hours**: time the machine's PLC said it was running (the dashboard's Machine running tag), measured by Ghost Map's background reads. **Powered-on hours**: time the PLC could be read at all.
- **Calendar days**.

An item turns "due soon" at 90% of its interval (change it per item) and "overdue" past it. When adding an item you can say when it was last done and how much it was already used, for example the hits a die already has. **Mark done** logs who did it, when, the reading at the time and a note, and starts the count again. Admins add, edit and mark items done; everything is in the audit log. Items are saved in `~/.ghostmap/maintenance.json`.

**Maintenance > Counters** shows a TSC line's feet, pieces, parts, production hours and scrap over the last 24 hours, 7 days, 30 days and all time, hole hits per station and tool for the last 30 days and all time, and the run and powered-on hours of every machine dashboard. Ghost Map's hour meters only count while it is running and can read the PLC, so they trail the machine's own hour meter; they are kept for good in `history.db` (not pruned with the alarm history).

### Moving to another PC

**Export everything** (Production > Connection) saves every machine dashboard (layout, flipped areas, hidden tags and the tag list behind it) and the TSC settings in one JSON file; **Export** on the Machine toolbar saves just the one dashboard. **Import...** adds the dashboards from such a file, optionally pointed at another gateway, and never overwrites one that's already there. Passwords and recorded history are never exported: enter the SQL password again after an import.

Upgrading GhostMap.exe keeps everything in `~/.ghostmap`, or `C:\ProgramData\GhostMap` when it runs as a service (dashboards, history, TSC settings, users). The version and build (commit) show next to the name in the top bar and in the debug bundle.

### Logins and remote access (IXON)

On a laptop, the UI is open to whoever sits at it, and the top bar shows **No login**. Click **Set up login** there to create the first admin (this only works on the machine itself), or use the command line. Admins manage further users from the **Users** button. To make it reachable from the network, for example through the IXON IXrouter's HTTP service, add a login and allow only the IXrouter:

```bash
ghostmap user add maint --role admin      # prompts for a password; any user turns login on
ghostmap user add operator                # viewer: can look, can't scan or probe
ghostmap web --host 0.0.0.0 --allow 192.168.1.1
```

Ghost Map refuses to serve beyond localhost without `--allow` and at least one login. A browser it turns away gets `Forbidden`; its address goes to `audit.log` and the alert list, so if IXON arrives from an address you didn't expect, that's where to find it. Logins and actions are written to `~/.ghostmap/audit.log`. The UI uses relative links, so it works under the path prefix IXON's proxy adds. Details are in [docs/OT-SAFETY.md](docs/OT-SAFETY.md#web-ui-access).

**HTTPS.** Add `--https` (to `web` or `service install`) so logins and session cookies don't cross the machine network in the clear. Ghost Map makes a self-signed certificate on first start (`tls\ghostmap.crt` in its data folder, valid 5 years, renewed automatically on a start in its last 30 days or after the PC is renamed) and prints its SHA-256 fingerprint. Browsers warn until the certificate is trusted: compare the fingerprint, or import `ghostmap.crt` into the browsing PC's Trusted Root Certification Authorities. To use a certificate from the plant's CA instead: `--https --cert ghostmap.pem --key ghostmap.key`. With HTTPS on, plain `http://` to the same port gets no answer, so set the IXON HTTP service to HTTPS on port 8470. Upgrading a service to HTTPS is `GhostMap.exe service install --https` (plus the same `--host`/`--allow` as before).

## Preparing a Stratix switch

Ghost Map needs read-only SNMP. These are example IOS / IOS-XE settings (Stratix 5200/5400/5410/5700/5800, or the equivalent in Device Manager / the WebUI):

```
! v2c, restricted to the engineering laptop
access-list 10 permit host 192.168.1.250
snmp-server community <ro-string> RO 10

! or v3 (preferred)
snmp-server view GHOSTMAP iso included
snmp-server group GHOSTMAP v3 priv read GHOSTMAP
snmp-server group GHOSTMAP v3 priv context vlan- match prefix read GHOSTMAP
snmp-server user ghostmap GHOSTMAP v3 auth sha <auth-key> priv aes 128 <priv-key>
```

**Several switches on one machine** (e.g. a line of Stratix 5200s, which run IOS-XE): list them all under *Switch IPs*, or just give SNMP credentials and let discovery add every Stratix it finds. All switches are read with the same credentials and at the same time. Ghost Map works out which ports link switches together (from LLDP/CDP, or from the switches' own MAC addresses when both are off), so each device is placed on the switch port it is actually plugged into.

**Optional, for Network > Live**: let the switch tell the HMI when something happens, by sending syslog and traps to it (192.168.1.250 here is the HMI). This is a change you make on the switch; Ghost Map never changes switch settings.

```
logging host 192.168.1.250
logging trap warnings
snmp-server host 192.168.1.250 version 2c <trap-string>
snmp-server enable traps snmp linkdown linkup coldstart warmstart authentication
```

IOS exposes the MAC address table per VLAN, so Ghost Map reads the per-VLAN tables as well. It uses `community@<vlan>` indexing for v2c and the `vlan-<id>` context for v3, which is why the v3 group above includes the `vlan-` context. If the switch supports Q-BRIDGE-MIB, one walk is enough.

**Switch health.** Each scan also reads the switch's CPU load (5 s, 1 min and 5 min averages, CISCO-PROCESS-MIB), memory (CISCO-MEMORY-POOL-MIB, or CISCO-PROCESS-MIB on IOS-XE), temperatures, fans and power inputs (CISCO-ENVMON-MIB, or ENTITY-SENSOR-MIB), and spanning tree topology changes per VLAN (BRIDGE-MIB). They show as tiles above the faceplate on the Switches page. Ghost Map flags a 5-minute CPU average of 80% or more, memory 90% or more used, a sensor or power input that isn't normal, and a spanning tree change in the last hour, since every change can drop EtherNet/IP I/O connections. A switch that doesn't support one of these MIBs simply shows fewer tiles.

**Traffic.** A scan reads the port counters twice, 10 seconds apart, and shows each port's bandwidth in and out (and how busy the link is), broadcast and multicast packets per second, and errors and drops per second. It flags a port that is 70% busy or more, a device sending 500 broadcasts/s or more (5,000 is a storm), input errors still climbing, and output drops. Change the window with *Traffic sample* in the scan dialog or `--traffic-window` (0 skips it).

**Traffic monitor.** Storms come and go, so a scan can miss them. Under **Network > Traffic**, an admin enters the machine's switch IPs and a read-only SNMP login once (kept sealed like the TSC password). Ghost Map then reads the counters every 30 seconds, whether or not anyone has the page open, and keeps 7 days of rates in `network.db`. It records every broadcast storm, busy link, climbing error count, output drop and high switch CPU as an event, with when it started, how long it lasted and its peak, kept 90 days. The page shows every port now, 24-hour peaks, charts per port (bandwidth, broadcast and multicast, switch CPU) and the event list, with a CSV download. With `--demo` it watches two simulated switches, the second daisy-chained off the first, with a looped laptop switch on the HMI port storming for a minute every half hour and a camera that keeps its 100 Mbps port busy.

**Daisy-chained switches and drives.** With several switches, the Traffic page lists them in wiring order, each indented under the port it hangs off (found from LLDP/CDP, or from the other switch's MACs), and each link port says which switch and port it goes to. A port with several devices behind it (drives daisy-chained through their two-port switches, a DLR ring, an unmanaged switch) is shown as "6 devices" with their names from the newest scan, and is not mistaken for a link to another switch when the devices are mostly drives and I/O (by MAC maker). A storm crossing a link between switches is shown greyed on the link, pointing at the port where it started, so one storm reads as one problem. A ring of drives shows as the ports at its two ends; telling whether a DLR or REP ring is broken needs the ring supervisor, not switch counters.

## Firmware baseline

Pass `--baseline file.json` to flag devices whose firmware differs from the machine standard. See [docs/baseline.example.json](docs/baseline.example.json). Rules match on `product_name`, or on `vendor_id` + `product_code`.

## Extending vendor names

The built-in CIP vendor table covers common automation vendors. To add the full ODVA vendor list, drop `{"<id>": "<name>", ...}` into `ghostmap/data/vendors.json` or point `GHOSTMAP_VENDORS` at such a file.

## Development

```bash
pip install -e ".[dev]"
pytest
python packaging/build_exe.py      # build dist/GhostMap.exe locally (pip install pyinstaller first)
```

The switch collector, correlation, and diagnostics all run against an in-memory SNMP agent (`FakeSnmpClient`) built from the simulated machine in `ghostmap/sim/machine.py`, so features can be developed without hardware. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status and roadmap

The full plan (resident HMI service, IXON access, OEE and machine profiles) is in [ROADMAP.md](ROADMAP.md).

v0.1 has been tested against the built-in simulators and a real SNMP agent (snmpsim) serving the simulated Stratix. **It has not yet been validated against physical Stratix or Logix hardware.** The first site test should start with `discover` and `switch` on one device.

Planned:
- CIP explicit messaging reads (Ethernet Link object: negotiated speed/duplex and media counters from the *device* side; TCP/IP object: DHCP/BOOTP mode, hostname, gateway)
- Logix backplane browse (modules, slots, firmware in each chassis)
- Stratix specifics: DLR ring status, Device Manager/CIP port-to-device mappings, IOS-XE (Stratix 5800) MIB differences
- Topology diagram view, PDF/Excel commissioning report
- Code-signed exe / installer

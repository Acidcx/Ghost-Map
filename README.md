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
  - **Tags**: a read-only tag browser in the style of UaExpert, for FactoryTalk Linx Gateway or any OPC UA server. Type an endpoint (a bare IP works; FT Linx Gateway's default port 4990 is added), optionally pick a security policy and login, and connect. Browse the address space, see a node's attributes, double-click tags to watch them live, and **Export tags** to get every tag under a node as CSV, which is handy for comparing naming between machines. Type `demo` as the endpoint (or start with `ghostmap web --demo`) to connect to a simulated gateway with two presses whose tag names drift.
- **Production** (SQL, from the TSC database)
  - **Line**: the line's part schedule (see below).
  - **Connection**: the TSC database settings, what the SQL login can read, and moving dashboards and settings to another PC.
- **Maintenance** (counters from TSC and the PLCs)
  - **Service**: service items with an interval (strokes, pieces, feet, hours or days), due-soon warnings and a log of the work done.
  - **Counters**: feet, pieces, production hours and punch strokes per tool from TSC, and run hours per machine from the PLC.
- **Network** (TCP/IP: EtherNet/IP and SNMP scans)
  - **Overview**: counts and all findings, with a hint for each.
  - **Devices**: a sortable, filterable inventory. Click a device for its full CIP identity, decoded status word, related findings, and a one-click probe.
  - **Switches**: a port faceplate coloured by health, a port table, and per-port detail (counters, neighbours, MACs mapped to devices).
  - **Compare**: the diff of any two scans.
  - **Probe**: a single-device check.

**Export CSV** downloads the device inventory.

### Machine dashboards

In **Tags (OPC UA)**, connect, select the controller (or just its fault folder) and press **Build dashboard**. Ghost Map reads every tag under it and lays out a dashboard: one card per folder (area) with its alarms, worst first, plus timers, fault words, counters and run state. Or press **From CSV...** on the Machine tab and pick an **Export tags** file. Tag names drift between machines, so the layout leans on structure first (TIMER and COUNTER members, data types, the folder a tag sits in) and on names second (`_Warn`, `E_Stop`, `_MS`, `Comms_Flt`, `OverTemp`...). Where it isn't sure it says so: for example, an area whose bits are mostly on when exported may use "on = healthy" (comms OK bits). Motion axes (AXIS_CIP_DRIVE, about 600 members each) collapse into one item showing state, enable/homed, position, velocity and fault words; **Which faults?** reads the axis's fault, alarm and inhibit bits once, on request. To keep live reads light, I/O module tags, arrays outside fault folders and long lists of status bits are left out (the dashboard says how many). Tick **Edit** to flip an area, rename or remove items and areas, change the tag behind an item, or add tags. Tags are picked from the ones found when the dashboard was built, so nothing is typed in by hand. Long names are cut short with "..."; hover for the full name and the raw tag. **Rebuild layout** (in Edit) lays an older dashboard out again with the current rules from the tags stored when it was built, keeping its running tags, heartbeat and flipped areas.

A whole controller is laid out in three levels: **Overview** (the tiles, everything active right now, and one card per PLC program), **Drives** (every motion axis in one table, grouped by program, faulted first; **Details** opens an axis's active faults, alarms and inhibits, the status bits that are on, and motion, power, limit, tuning and fault-word values, refreshed while open), and one page per program (per controller and program when the dashboard spans several PLCs) with its area cards grouped by top folder, a filter, an "only areas with something active" switch, and a compact Signals panel. One-shot storage (`OSg`, `OS1`, `ONS`), command bits in fault folders (`Reset_Faults`) are left out, and bits like `No_Faults` count as active when off. FT Linx Gateway lists a program's InOut parameters and aliases as tags of their own, so the same UDT can appear a dozen times; copies of controller-scope tags are dropped (same members, types and BOOL values). Add-On Instruction EnableIn/EnableOut, MSG and motion instruction tags, strings and status bits with no recognisable job are left out too, and stay searchable in Add tag. The **Machine** tile reads the running tag Ghost Map guessed (it prefers `Running` or `RunF` over names like `AutoBatchRunout`); in Edit, its Edit button lets you add or remove running tags and choose whether any or all of them must be on. Live values are read by the Ghost Map service (read-only, at most once a second, shared by everyone watching), over one OPC UA session per gateway however many dashboards use it. Dashboards are saved in `~/.ghostmap/dashboards`.

**Alarm history and first-out.** While Ghost Map runs, it reads every saved dashboard about once a second in the background, whether or not anyone has a page open, and records each alarm (and faulted motion axis) from when it went active until it cleared. A **stop** runs from the first alarm after a clear machine until every alarm has cleared; the alarm that started it is its **first out**, marked on the Overview while the stop is on. Reads are about a second apart, so alarms that go active within the same second are shown as a tie. The **History** page lists the stops with their first-out, the alarms that are first-out, active or active longest most often, the alarm log, and comms gaps (reads that failed: alarm states are held through a gap, never guessed), over the last 8 hours to 90 days, with **Download CSV**. History is a SQLite file, `~/.ghostmap/history.db`, kept for 90 days; open it with DB Browser for SQLite if you want to query it yourself. Ghost Map writes only to that file, never to a PLC or gateway. `ghostmap web --no-collect` turns background reading and history off.

**Can the dashboard be trusted?** The **Comms** tile and the **Health** page show how many of the dashboard's tags read Good (per PLC, with the status the gateway returned for the rest), read time, drops and reconnects, and a heartbeat: a tag the PLC changes all the time (Ghost Map picks one named like `Heartbeat` or `Watchdog`; choose your own in Edit, preferably a counter). If the heartbeat stops changing, the data may be frozen even though every tag reads Good, so faults show "?" instead of OK. If there's no reliable heartbeat, set **Freshness check** to **Response time only** (Health page, in Edit): data then counts as fresh while every read comes back within the limit you set (default 2000 ms). A tag that can't be read shows grey, never OK. **Check all alarms** reads every alarm once and lists the ones that can't be trusted: tags the gateway doesn't know (renamed in the PLC), tags it can't read, alarms that aren't BOOLs, duplicates, and areas where most bits are on.

**Debug log.** Ghost Map writes its own log to `~/.ghostmap/logs/ghostmap.log`: server errors with stack traces, OPC UA drops and reconnects, and uncaught errors from the browser pages. Admins open it with **Debug log** in the top bar, and **Download debug bundle** zips it with versions and comms health (no tag values, but it does contain gateway addresses and user names).

Scans are stored as JSON in `~/.ghostmap/scans` (override with `--data-dir` or `GHOSTMAP_DATA`). SNMP credentials are never written to disk.

### Production (TSC part schedule)

**Production > Line** shows one line from the TSC part schedule. TSC's words: an **order** holds jobs, a job holds **bundles**, a bundle holds **part lines** (one row of the cut list: a profile, a length and a quantity), and each part line is made as **pieces**. The page shows, for this shift: orders worked, pieces and feet made, feet per hour, uptime and stops with TSC's stop reasons, scrap pieces and remakes; one card per **station** (a separate machine on the line, such as a punch, a notcher, or the pan and back skin rollformers that feed a sandwich press, which waits for both); the part running now with its progress at each station and a finish estimate; parts on hold; feet per clock hour for the last 12 hours; the orders on the schedule with bundles, pieces done of the whole order and feet left; the queue in TSC's run order; and the last 15 part lines finished. It refreshes every 15 seconds; the server reuses each read for 30 seconds, so many people watching cost one query.

It reads TSC's own views with fixed SELECTs on a read-only connection (`ApplicationIntent=ReadOnly`): `DataView.vHmiPartScheduleQueueOrInProgress` for the queue (status 2 queued, 3 on hold, 4 in progress, ordered by `QueueIndex`), `DataView.vPartScheduleCompleted` for finished parts (status 5) and `DataView.vPartScheduleCommon` for the lines and whole-order totals. Imported and pending parts (status 0 and 1) aren't in the queue until TSC queues them. TSC's `1900-01-01` means "no time yet". Customer names and comments are never read, and Ghost Map never reads `dbo.Order`.

With a few more read grants it also shows stations (`dbo.Station`, `dbo.StationPart`, `dbo.StationDependency`), the shift from TSC's calendar (`dbo.Shift`, `dbo.DayOfWeek`; otherwise the shift start times in the settings), stops and reasons (`dbo.vGetDownTimeData`) and punch strokes (`dbo.PartPattern`, `dbo.PatternHole`, `dbo.Hole`, `dbo.PartNotch`, `Machine.ToolType`). Without them the page still works; **What Ghost Map can read** on the Connection page lists what each one adds and the exact GRANT to run. None of these tables hold customer data.

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

- **Punch strokes** at a station, for one tool type or all of them: pieces made times the holes and notches on each part (TSC's patterns, holes and notches; a repeat pattern counts once per repeat along the part's length).
- **Pieces made** (each piece is a shear cut), **feet made**, or **production hours** (start to end of each part) on a TSC line.
- **Run hours**: time the machine's PLC said it was running (the dashboard's Machine running tag), measured by Ghost Map's background reads. **Powered-on hours**: time the PLC could be read at all.
- **Calendar days**.

An item turns "due soon" at 90% of its interval (change it per item) and "overdue" past it. When adding an item you can say when it was last done and how much it was already used, for example the strokes a die already has. **Mark done** logs who did it, when, the reading at the time and a note, and starts the count again. Admins add, edit and mark items done; everything is in the audit log. Items are saved in `~/.ghostmap/maintenance.json`.

**Maintenance > Counters** shows a TSC line's feet, pieces, part lines, production hours and scrap over the last 24 hours, 7 days, 30 days and all time, punch strokes per station and tool for the last 30 days and all time, and the run and powered-on hours of every machine dashboard. Ghost Map's hour meters only count while it is running and can read the PLC, so they trail the machine's own hour meter; they are kept for good in `history.db` (not pruned with the alarm history).

### Moving to another PC

**Export everything** (Production > Connection) saves every machine dashboard (layout, flipped areas, hidden tags and the tag list behind it) and the TSC settings in one JSON file; **Export** on the Machine toolbar saves just the one dashboard. **Import...** adds the dashboards from such a file, optionally pointed at another gateway, and never overwrites one that's already there. Passwords and recorded history are never exported: enter the SQL password again after an import.

Upgrading GhostMap.exe keeps everything in `~/.ghostmap` (dashboards, history, TSC settings, users). The version and build (commit) show next to the name in the top bar and in the debug bundle.

### Logins and remote access (IXON)

On a laptop, the UI is open to whoever sits at it, and the top bar shows **No login**. Click **Set up login** there to create the first admin (this only works on the machine itself), or use the command line. Admins manage further users from the **Users** button. To make it reachable from the network, for example through the IXON IXrouter's HTTP service, add a login and allow only the IXrouter:

```bash
ghostmap user add maint --role admin      # prompts for a password; any user turns login on
ghostmap user add operator                # viewer: can look, can't scan or probe
ghostmap web --host 0.0.0.0 --allow 192.168.1.1
```

Ghost Map refuses to serve beyond localhost without `--allow` and at least one login. Logins and actions are written to `~/.ghostmap/audit.log`. The UI uses relative links, so it works under the path prefix IXON's proxy adds. Details are in [docs/OT-SAFETY.md](docs/OT-SAFETY.md#web-ui-access).

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

IOS exposes the MAC address table per VLAN, so Ghost Map reads the per-VLAN tables as well. It uses `community@<vlan>` indexing for v2c and the `vlan-<id>` context for v3, which is why the v3 group above includes the `vlan-` context. If the switch supports Q-BRIDGE-MIB, one walk is enough.

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

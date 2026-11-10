# Changelog

Each version is a git tag (`v0.5.0`, ...). Pushing the tag builds `GhostMap.exe` and attaches it to a GitHub
Release. `GhostMap.exe service status` and the top bar name the version and commit that are installed.

## 0.5.6 (2026-11-10)

- **Fix: Network > Live dropped every packet on a PC with no scan yet.** With no saved scan, the lookup that names
  the devices a syslog message, SNMP trap or BOOTP request came from never filled its cache, so every datagram the
  passive listeners heard raised `KeyError: 'labels'` in the log and was thrown away. Seen on a fresh install on an
  engineering PC. Live now records those packets before the first scan too.

## 0.5.5 (2026-11-10)

- **No more `arp.exe` every few seconds.** The passive ARP read shelled out to `arp.exe` every 10 s, which flashed
  a console and, as a service launching the same process over and over, looked like malware to Windows Defender's
  behaviour heuristics (and cost a process spawn each time). It now reads the ARP table through the Windows IP
  helper API directly, with no child process. On Linux it still reads `/proc/net/arp`; macOS/BSD still use `arp`.
- **No more `powershell.exe` for host health.** The This PC page read the FactoryTalk/Rockwell/SQL/TSC services and
  the Windows event-log errors by launching `powershell.exe` every 5 minutes — the other repeated child process
  that read as malware-like and that Defender's real-time protection re-scanned each time. Ghost Map now reads them
  in-process through the Windows service-control and event-log APIs (read-only: it enumerates and queries, never
  starts, stops or changes a service or clears a log). The page shows exactly what it did before. Off Windows these
  are no-ops, as they were.
## Unreleased

- **GhostMap-Setup.exe.** An installer next to the single-file exe: it puts an unpacked build (GhostMap.exe +
  `_internal`) in `C:\Program Files\GhostMap` and sets up the service, upgrades in place keeping the data and the
  service options, and uninstalls from Settings > Apps keeping the data. The unpacked build doesn't unpack itself to
  `%TEMP%` on every start, so antivirus checks its files once instead of each time, and it runs as one process
  instead of two. Silent install for several PCs: `/VERYSILENT /SUPPRESSMSGBOXES [/SERVICEARGS="--ixon"]`.
  `service install` from the unpacked build copies its libraries too. CI installs, upgrades and uninstalls it.
- **Code signing, ready for a certificate.** `packaging/sign_exe.ps1` signs and timestamps GhostMap.exe, and the
  exe build signs it when the `GHOSTMAP_SIGN_PFX` secret is set (unsigned with a notice otherwise; a throwaway copy
  is signed on every build to test the script). `docs/CODE-SIGNING.md` compares the certificate options.
- **Cloned PC serials are flagged.** Two PCs imaged from one disk answer as RSLinx / FactoryTalk Linx (vendor 77)
  with the same CIP serial, and a PLC can then drop their connections in turn. A scan now reports this as an error,
  `device.cloned_serial`, with the fix in its hint. Two different devices sharing a serial get the same error;
  one device answering on two IPs is still the `device.multi_ip` info.
- **Cloned PCs are no longer merged.** Devices were tracked by vendor + serial, so two cloned PCs shared one key:
  Compare could drop one and clicking one in Devices could open the other. Linx PCs are now tracked by hostname.
- The demo machine has two HMI PCs, cloned in the "today" scan.
## 0.5.4 (2026-11-05)

After Ghost Map was seen using about 50% CPU on a Test HMI (killed and restarted before anything was captured).

- **Ghost Map watches its own CPU.** A small thread samples Ghost Map's own CPU time every 5 s. If it keeps 60% of a
  core busy for a minute, it writes what every thread is doing to the debug log (at most every 30 minutes) and the
  alert list shows **hmi.ghostmap_busy**. One pinned core is only 50% in Task Manager on a two-core HMI, under the
  existing 90% CPU alert, so this was invisible before. Settings > This PC shows "Ghost Map itself" under CPU.
  If it happens again, download the debug bundle before restarting.
- **Guard against a Windows asyncio spin.** asyncio's ProactorEventLoop wakes itself through a socket pair; when
  something outside Python (a network filter driver or security software) shuts it down, the loop re-reads it
  forever at 100% of a core until restarted (CPython gh-156333, not fixed upstream). Ghost Map now makes a new pair
  and logs a warning instead. Unconfirmed as the cause on the HMI: the log line will say if it was.
- **No more "CPU 100% busy for 5 minutes" right after a reboot.** Seen on the Test HMI: the service started two
  minutes after boot, its first CPU sample covered the one second since it started (100% while Windows and
  FactoryTalk were still starting), and the alert fired 12 s later. A CPU sample now spans at least 20 s and the
  5-minute average needs four samples.
- **One PowerShell read at a time.** Pages opened together right after a restart (Home, the alert bar, the start-up
  warm-up) each started their own services and event-log PowerShell read; they now share one.
## Unreleased

- **Machine > Device health.** One page with everything Ghost Map can already read about each drive and device, worst
  first, each problem with a dotted code and what to check:
  - **Drives** (Kinetix / CIP Motion axes, through FT Linx): the drive's thermal model for the motor, inverter,
    converter and bus regulator (% of rated; over 80 % warns, 100 % is an error), DC bus voltage, output current and
    power, axis state, and the over-temperature and thermal-overload alarm and fault bits that are on. Kinetix drives
    don't send temperatures over the network: capacity is the closest reading.
  - **Controller** from FT Linx's Diagnostic Items: mode (warns when not in Run), firmware, Linx status and connection
    drops. **I/O modules**: each module's `:I.Fault` word (all ones = no connection) and produced/consumed tags'
    `ConnectionFaulted`.
  - **Network devices** from the last scan: CIP fault and I/O connection status, firmware that differs from the other
    devices of the same product (or the baseline), and a serial that answers from more than one address (an error
    for FactoryTalk Linx PCs from a cloned image).
  Read-only, only while the page is open, one read per machine every 5 s at most however many people watch.
- **CPU fix: a drive's Details panel parsed the whole tag export on every refresh.** Machine > Dashboard > Drives
  > Details refreshes every 2 s, and each refresh re-read the dashboard's stored tag export (about 30 MB for a whole
  controller, ~0.5-1 s of CPU). A few open Details panels kept Ghost Map's CPU high. The export is now indexed once
  and kept until it changes.

## 0.5.3 (2026-11-05)

For machines that don't read their switches over SNMP.

- **Switch pages only when SNMP is in use.** Network > Switches and Traffic, the switch tiles on the Scan page, the
  SNMP field in Probe and the Home card's "Switches" line only show once a stored scan has read a switch or the
  traffic monitor is set up. The SNMP part of the New scan dialog is folded away under "Switches over SNMP
  (optional)"; a scan that reads a switch turns the pages on. Nothing was removed: the SNMP code is unchanged.
- **New scan could not start with its defaults.** Reply wait defaulted to 2 s with a step of 0.5 from 0.2, which
  the browser counts as invalid, so Start scan only showed "Please enter a valid value" until the number was
  changed. The step is now 0.1, and a test checks every number field's default.
- **docs/SNMP-SETUP.md**: step-by-step read-only SNMP setup for a Stratix, for when a site wants the switch pages.

## 0.5.2 (2026-11-05)

Security hygiene before the team uses Ghost Map on a machine.

- **HTTPS by default on the network.** `web --host 0.0.0.0` and `service install --ixon` (or `--host`) serve HTTPS
  with the self-signed certificate unless `--http` is given; localhost-only stays plain HTTP. Upgrading a service
  that was open to the network over plain HTTP switches it to HTTPS and says so: set the IXON HTTP service for Ghost
  Map to HTTPS, port 8470, and open `https://127.0.0.1:8470` on the HMI.
- **No DNS rebinding.** A request from the HMI itself is refused unless it names Ghost Map by IP, `localhost` or
  the PC's name. Before, a web page open on the HMI could point its own domain at 127.0.0.1 and use Ghost Map with
  the HMI's local access (admin while no login exists).
- **Exe checksum.** Every build and release has `GhostMap.exe.sha256`; compare it with `Get-FileHash GhostMap.exe`.
- **Pinned CI actions.** The GitHub Actions the build uses are pinned to exact commits, so a moved tag can't
  change what builds the exe.

## 0.5.1 (2026-11-05)

Fixes from the first days on a real machine.

- **Less like malware to Windows Defender.** Defender's machine-learning check (`Behavior:Win32/Execution.A!ml`)
  deleted the exe on a work laptop. Ghost Map no longer starts PowerShell with a base64 `-EncodedCommand`: the
  read-only service and event log query is plain text on the command line, where anyone can read it. The exe now
  carries Windows version information (Properties > Details) and is built without UPX. Code signing remains the
  real fix; until then, ask IT for an exception and report the false positive to Microsoft.
- **Device labels.** Kinetix 5700 drives and power supplies are named from their catalog number ("Servo Drive
  (Kinetix 5700)", "Servo Power Supply (Kinetix 5700)") instead of "Device type 0x2D/0x30". Devices that send a
  one-letter product name show "Product code N (sends "P" as its name)", and the raw reply goes to the debug log.
  A device that states too short an identity item no longer has its name cut off.
- **Devices tab.** Clicking a device no longer rebuilds the table (the page jumped); the details panel stays in
  view beside a long list (on a phone it scrolls into view); the filter, the EtherNet/IP-only box and the sort
  survive a page reload.

## 0.5.0 (2026-10-29)

Ready for the first test on a real machine.

- **Alerts you can trust during commissioning.** Admins can acknowledge an alert (until it clears or gets
  worse), mute it for 8 hours to 7 days, or mute every alert with the same code (all `machine.stopped` while the
  machine is still being commissioned). Muted alerts leave the banner and the count but stay in the list and the
  logs. Mutes survive a restart and always end, after 30 days at most.
- **Services that matter.** SQL Server (the engine, e.g. `SQL Server (SQL2022)`) and TSC's services are watched
  next to FactoryTalk's, and SQL Server, TSC, Linx, Tag Server and Alarm services stopped are errors. A service
  set to start manually that was running in the last day counts as stopped, and an alert says how often a service
  stopped in the last 7 days.
- **Quick first page after a reboot.** The service reads this PC's services and event log, TSC's lines and
  production and the alert list a few seconds after it starts.
- **Same exe for the same tag.** `GhostMap.exe` is built with the exact dependency versions of the build tested on
  the Test HMI (`packaging/exe-constraints.txt`). `service status` names the installed version and commit, and
  says when the exe you ran it from is a different one.
- `docs/SITE-TRIAL.md`: what to check on the day, after a day and after a week on a real machine.
- Daylight saving in the production history is documented: the repeated hour when the clocks go back is one row.

Also since 0.4.0 (PR #7): `service install --ixon`, `service uninstall --purge`, the TSC event log viewer, the
Home page and the regrouped tabs.

## 0.4.0 (2026-10-13)

OPC UA subscriptions, the Windows service, the alert list, HTTPS and production history (PR #6).

# Changelog

Each version is a git tag (`v0.5.0`, ...). Pushing the tag builds `GhostMap.exe` and attaches it to a GitHub
Release. `GhostMap.exe service status` and the top bar name the version and commit that are installed.

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

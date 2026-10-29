# Changelog

Each version is a git tag (`v0.5.0`, ...). Pushing the tag builds `GhostMap.exe` and attaches it to a GitHub
Release. `GhostMap.exe service status` and the top bar name the version and commit that are installed.

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

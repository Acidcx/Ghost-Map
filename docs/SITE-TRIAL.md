# Site trial checklist

What to check when Ghost Map goes onto a real machine, from the day it's installed to a week later. Install it
first as in the README (`service install`, a login, then `service install --ixon` for IXON access).

Keep everything you collect (debug bundles, screenshots, scans, tag lists) off the repo: it's real plant data.
Anything worth a regression test gets replayed with made-up data in `ghostmap/sim/`.

## On the day

- [ ] `GhostMap.exe service status` names the version and commit you meant to install.
- [ ] Home loads in a few seconds, also right after a reboot (the caches are warmed just after the service starts).
- [ ] Maintenance > This PC lists the FactoryTalk, Linx, SQL Server and TSC services with the state you expect.
- [ ] One switch first: `GhostMap.exe switch <ip>` reads ports, speeds and the MAC table with the read-only
      community, and nothing on the network minds. Then a full scan; save it as the baseline.
- [ ] OPC UA: the gateway certificate is pinned, a small dashboard (heartbeat, run state, fault word, a counter)
      shows the same values as Studio 5000, and FT View screens don't update slower than before.
- [ ] Alerts that are expected during commissioning are acknowledged or muted (Alerts > Acknowledge or mute...),
      so the banner only shows what's new. Mutes end on their own; a month at most.
- [ ] Download a debug bundle (System > Logs) to compare with later.

## After a day

- [ ] The service is still running and hasn't restarted (`service status`; System event log, source Service
      Control Manager).
- [ ] The alert list: each alert is real, or muted on purpose. Anything that cries wolf is a rule to fix.
- [ ] Run and stop on the machine page follow the machine, and a real fault shows the right first-out.

## After a week

- [ ] **FactoryTalk Linx load:** CPU and memory of the Linx and Linx Gateway processes in Task Manager > Details,
      against what they used before Ghost Map. FT View should feel no different.
- [ ] **Data size:** the size of `history.db` and `production.db` in `C:\ProgramData\GhostMap`. A week's growth
      times 13 is roughly the 90-day size.
- [ ] **Alerts per day:** Event Viewer > Application, filter on source GhostMap, or in PowerShell:
      ```powershell
      Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='GhostMap'; StartTime=(Get-Date).AddDays(-7)} |
        Group-Object { $_.TimeCreated.Date.ToString('yyyy-MM-dd') } | Select-Object Name, Count
      ```
      Note how many were noise. A handful a day that each meant something is the goal.
- [ ] **First-out order:** for a few real stops, the first-out alarm Ghost Map shows matches what maintenance found.
- [ ] **Services:** any service that stopped more than once ("stopped N times in the last 7 days" on the alert).
- [ ] **Switches:** CRC, duplex and storm events on Network > Traffic, and whether the device-to-port map still
      matches the machine.
- [ ] Download another debug bundle and keep both with the trial notes.

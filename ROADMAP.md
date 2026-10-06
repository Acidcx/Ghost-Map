# Ghost Map roadmap

Goal: a per-machine "nervous system". One install on each machine shows network health, device health, OEE and maintenance status. The same installer rolls out to every machine, and is reachable remotely through IXON.

## Decisions so far

| Topic | Decision | Why |
|---|---|---|
| Where Ghost Map runs | **On the machine's Windows 11 LTSC HMI**, as a background Windows service | The machines have IXrouter3, which provides remote access but can't host apps. Docker Edge Apps need an IXON SecureEdge Pro. The HMI is already on the machine network and has a screen. |
| Remote access | **IXON IXrouter3 HTTP service** → HMI `:8470` | Outbound-only connection, no inbound firewall ports, and IXON handles user login and audit. |
| Who can reach the web UI | Only `localhost` (the HMI screen) and the IXrouter's LAN IP, plus a Ghost Map login | Once it listens on the machine network, it must not be open to every device on the LAN. |
| Device access | **Read-only, always**: ListIdentity, SNMP GET, Logix tag *reads*. The code has no write paths. | OT trust. Remote reachability makes this even more important. |
| Machine data source | Ghost Map reads the PLC directly over EtherNet/IP. It does not go through the HMI software. | Works whatever the HMI runtime is, and survives HMI application restarts. |
| Storage | SQLite on the HMI, with raw data aggregated to minutes and a retention limit (e.g. 90 days) | HMI disks are small, and there's no extra service to install. |
| Future host option | Same code packaged as an ARM64 Docker image if machines move to SecureEdge Pro | Fleet updates from IXON Cloud. |

## Phase 1: finish the commissioning tool (laptop / exe)

- [x] EtherNet/IP discovery, Stratix / IOS-XE switch reading, port mapping, diagnostics, scan diff
- [x] Multiple switches per machine; non-EtherNet/IP hosts listed with MAC vendor; RSLinx PCs labelled
- [ ] SNMP identification of non-CIP hosts (sysDescr: model / firmware of Moxa, Netgear, IT switches)
- [ ] Device-side Ethernet diagnostics (CIP Ethernet Link object: speed, duplex, error counters)
- [ ] Fill in the scanner PC's own MAC address
- [ ] Relative URLs in the web UI so it works behind the IXON HTTP proxy
- [ ] Topology diagram; PDF commissioning report
- [ ] PROFINET DCP discovery for Siemens machines (needs Npcap on Windows)

## Phase 2: resident service on the HMI

- [ ] `GhostMap.exe service install|uninstall|start|stop` to run as a Windows service at boot
- [ ] Scheduled background scans and polling, with history in SQLite
- [ ] Change timeline: device added or removed, firmware changed, port down, new MAC on a port
- [ ] Alerts: thresholds and rate of change (e.g. CRC errors climbing); notifications through the IXON-approved path
- [ ] Login and roles (viewer / maintenance / admin); listen only on localhost plus the IXrouter LAN IP
- [ ] HMI self-health: disk, uptime, Windows event-log errors, RSLinx / FactoryTalk / HMI-runtime services running

## Phase 3: machine data, OEE and maintenance

- [ ] Logix tag reads (read-only, rate-limited, batched)
- [ ] **L5X import**: read the Studio 5000 project export for fault UDTs, member descriptions and fault message text, so no fault tables are typed by hand
- [ ] **Machine profiles** (see below), with automatic profile and variant detection
- [ ] OEE: availability × performance × quality per shift, day and week
  - Performance from **shear / press fire counts** against the ideal cycle time
  - Availability from run state against planned time (shift calendar per machine)
  - Quality from good/reject counts, where the PLC has them
- [ ] Downtime tracking: first-out fault, duration, Pareto by fault
- [ ] Maintenance counters: blade / die / cylinder life by stroke count, runtime hours, and "due soon" warnings
- [ ] Machine dashboard: an andon-style status screen on the HMI, plus the same page via IXON

## Phase 4: cross-layer correlation ("the brain")

- [ ] One timeline that combines network, device and process events (e.g. press faulted with drive comms loss ← CRC errors on the drive's port for the previous hour)
- [ ] Trend-based early warnings (cable degrading, fault frequency rising)
- [ ] Firmware security advisories (Rockwell / CISA) and product lifecycle status, bundled offline
- [ ] Optional plant-level rollup across machines; optional MQTT / OPC UA publishing

## Machine profiles (for automatic rollout)

One installer is used on every machine. On first start Ghost Map:

1. Scans the machine network and finds the controller(s).
2. Reads the controller's identity, and its tag and UDT list (read-only browse).
3. **Matches a profile** from the bundled profile library by controller name pattern, and by which tags and UDT layouts exist. Fault structures are similar across machines but have variants, so a profile is a *base* plus *variants*. The variant is chosen by comparing the actual UDT members to each variant's definition.
4. Starts OEE and maintenance collection with that profile, and shows **"profile: Press Line v2 (variant B), matched 14/14 tags"** on the dashboard. If nothing matches, it falls back to network and device health only and flags the machine for review.

A profile maps logical signals to tags. Draft format:

```yaml
profile: press-line
version: 2
match:
  controller_name: "PRESS_*"
  requires_tags: [PressFireCount, MachineState, Faults]
signals:
  cycle_count: PressFireCount        # DINT running counter (preferred over a one-scan pulse bit)
  shear_count: ShearFireCount
  running: MachineState.Auto_Running
  faults: Faults                     # fault UDT array, layout from the variant below
  good_count: PartsGood              # optional
  reject_count: PartsRejected        # optional
ideal_cycle_time_s: 2.4
maintenance:
  - name: Shear blade
    counter: shear_count
    interval: 250000                 # strokes
variants:
  A: { fault_udt_members: [Code, Active, Timestamp] }
  B: { fault_udt_members: [Code, Active, Timestamp, Station] }
```

Rules that keep this reliable:
- **Counters, not pulse bits.** A one-scan "fire" bit is invisible to any polling tool. A DINT counter in the PLC can't be missed. Machines that only have the bit need a one-line counter added.
- Profiles live in this repo, versioned, and ship with each release. The same profile on every machine of a type means the same dashboards everywhere.

## Updating machines

IXrouter3 can't push software, so updates go out by one of:
- Remote session to the HMI over IXON, then run the new installer.
- The plant's software-distribution tool, if the HMIs are managed.
- Later: Ghost Map checks a plant-local update share, for sites that allow it.

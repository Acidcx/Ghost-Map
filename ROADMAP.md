# Ghost Map roadmap

Goal: a per-machine "nervous system". One install on each machine shows network health, device health, OEE and maintenance status. The same installer rolls out to every machine, and is reachable remotely through IXON.

## Decisions so far

| Topic | Decision | Why |
|---|---|---|
| Where Ghost Map runs | **On the machine's Windows 11 LTSC HMI** (FactoryTalk View v16), as a background Windows service | The machines have an IXON IXrouter3, which provides remote access but can't host apps. The HMI is already on the machine network and has a screen. Docker on an IXON SecureEdge Pro is a possible later host, but unlikely. |
| Remote access | **IXON IXrouter3 HTTP service** → HMI `:8470` | Outbound-only connection, no inbound firewall ports, and IXON handles user login and audit. |
| Web face security | **Highest-risk part of the design, so it's built first.** Only `localhost` and allow-listed IPs (the IXrouter) can connect; Ghost Map logins with roles; an audit log. | Once the UI is reachable through IXON it's an outward-facing service on an OT asset (IEC 62443 conduit). |
| Device access | **Read-only, always.** No write, set, reset or configuration code paths. | OT trust. Remote reachability makes this even more important. |
| PLC tag data | **OPC UA client reading from FactoryTalk Linx Gateway on the same HMI.** Reads and subscriptions only, with a read-only OPC UA user. OPC UA is *not* used to publish Ghost Map data. | Works for every controller FT Linx can reach, including older ones without a built-in OPC UA server, and Ghost Map never opens its own connection to the PLC. |
| Detection | **Passive first.** Listen to what the HMI's own network card sees. Active traffic (ListIdentity, SNMP GET to the Stratix switches) only where passive can't answer the question, rate-limited and listed in `docs/OT-SAFETY.md`. | Minimal footprint on the machine network. |
| Capture without a mirror port | Machine OT network only, **no SPAN/mirror port**. Capture with Windows' built-in **pktmon** first; Npcap only if pktmon falls short. | pktmon needs no third-party kernel driver or license. Npcap's free license is limited to 5 systems per organization, and an extra driver has to be justified and patched under 62443. |
| Storage | SQLite on the HMI, with raw data aggregated to minutes and a retention limit (e.g. 90 days) | HMI disks are small, and there's no extra service to install. |

### What passive listening can see without a mirror port

On a switched network the HMI's network card only receives broadcast and multicast frames plus its own traffic:
- ARP (which hosts are alive, IP ↔ MAC), DHCP/BOOTP requests, gratuitous ARP on IP conflicts
- LLDP/CDP from the switch port the HMI is plugged into
- EtherNet/IP ListIdentity browses from other RSLinx / FactoryTalk Linx PCs, and the replies that are broadcast
- PC chatter (NetBIOS, mDNS, SSDP, LLMNR) that names engineering PCs
- The HMI's own traffic to the controllers

It can't see traffic between other devices (e.g. PLC ↔ drive I/O). Per-port errors and device health elsewhere still come from SNMP on the Stratix switches and from ListIdentity.

## Phase 1: commissioning tool (laptop / exe)

- [x] EtherNet/IP discovery, Stratix / IOS-XE switch reading, port mapping, diagnostics, scan diff
- [x] Multiple switches per machine; non-EtherNet/IP hosts listed with MAC vendor; RSLinx PCs labelled
- [ ] SNMP identification of non-CIP hosts (sysDescr: model / firmware of Moxa, Netgear, IT switches)
- [ ] Device-side Ethernet diagnostics (CIP Ethernet Link object: speed, duplex, error counters)
- [ ] Fill in the scanner PC's own MAC address
- [ ] Topology diagram; PDF commissioning report
- [ ] PROFINET DCP discovery for Siemens machines

## Phase 2: secure resident service on the HMI

Web face first, because it's the biggest risk:
- [x] Relative URLs in the web UI so it works behind the IXON HTTP proxy
- [x] Client allow-list: only `localhost` and listed IPs/subnets (the IXrouter) can connect
- [x] Logins with roles (viewer / admin), hashed passwords, session cookies, login lockout
- [x] Audit log of logins and actions (scans started, scans deleted)
- [x] Security headers; state-changing requests protected against cross-site requests
- [ ] HTTPS between the IXrouter and the HMI (self-signed certificate or plant CA)

Then the service:
- [ ] `GhostMap.exe service install|uninstall|start|stop` to run as a Windows service at boot
- [ ] Scheduled background scans and polling, with history in SQLite
- [ ] Change timeline: device added or removed, firmware changed, port down, new MAC on a port
- [ ] Alerts: thresholds and rate of change (e.g. CRC errors climbing); notifications through the IXON-approved path
- [ ] HMI self-health: disk, uptime, Windows event-log errors, FactoryTalk Linx / View services running

## Phase 3: passive detection

- [ ] Capture with pktmon (built into Windows), with filters for ARP, LLDP/CDP, DHCP/BOOTP, ListIdentity and name broadcasts
- [ ] Live device list from passive traffic: new, missing and changed devices without sending anything
- [ ] IP conflict and duplicate-MAC detection from ARP
- [ ] HMI uplink health from LLDP (switch name, port, VLAN) and its own interface counters
- [ ] Npcap as an optional capture backend if pktmon falls short

## Phase 4: machine data, OEE and maintenance (OPC UA)

- [x] OPC UA Tag Browser (UaExpert-style): endpoints, browse, attributes, live watch, tag-list CSV export; Security None or signed/encrypted with a self-signed client certificate
- [ ] Server certificate trust list (accept once, then pin)
- [ ] Background OPC UA subscriptions for the resident service
- [ ] **L5X import**: read the Studio 5000 project export for fault UDTs, member descriptions and fault message text, so no fault tables are typed by hand
- [ ] **Machine profiles** (see below), with automatic profile and variant detection from the OPC UA browse
- [ ] OEE: availability × performance × quality per shift, day and week
  - Performance from **shear / press fire counts** against the ideal cycle time
  - Availability from run state against planned time (shift calendar per machine)
  - Quality from good/reject counts, where the PLC has them
- [ ] Downtime tracking: first-out fault, duration, Pareto by fault
- [ ] Maintenance counters: blade / die / cylinder life by stroke count, runtime hours, and "due soon" warnings
- [ ] Machine dashboard: an andon-style status screen on the HMI, plus the same page via IXON

## Phase 5: cross-layer correlation ("the brain")

- [ ] One timeline that combines network, device and process events (e.g. press faulted with drive comms loss ← CRC errors on the drive's port for the previous hour)
- [ ] Trend-based early warnings (cable degrading, fault frequency rising)
- [ ] Firmware security advisories (Rockwell / CISA) and product lifecycle status, bundled offline
- [ ] Optional plant-level rollup across machines

## Machine profiles (for automatic rollout)

One installer is used on every machine. On first start Ghost Map:

1. Scans the machine network and finds the controller(s).
2. Browses the controller's tags and UDTs through the OPC UA server (FactoryTalk Linx Gateway), read-only.
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

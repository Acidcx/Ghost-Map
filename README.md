# Ghost Map

Ghost Map is a read-only device mapper and commissioning/debug tool for OT networks. Plug a laptop into the machine network and it tells you:

- **What's on the wire.** It finds every EtherNet/IP device and reads its vendor, product, device type, firmware revision, serial number, and CIP state and status (owned, I/O faulted, major or minor fault).
- **Where each device is plugged in.** It reads Stratix and other Cisco IOS managed switches over SNMP: model, IOS version, serial, per-port link, speed, duplex, VLAN, CRC/alignment/late-collision counters, MAC table, ARP table, and LLDP/CDP neighbours. It then maps each device to its switch port.
- **What looks wrong.** It flags duplicate IPs, faulted devices, half duplex or a duplex mismatch, cabling/EMI error counters, firmware that is off-baseline or mixed, daisy-chained or unmanaged-switch ports, NAT'd devices, and switch data it couldn't read.
- **What changed since last time.** It compares two scans: devices added or removed, swapped hardware (same IP, new serial), firmware or state changes, and new or resolved findings.

It comes as a CLI and a local web UI. Both work fully offline (no CDNs) and run on Windows, Linux, and macOS with Python 3.10+.

> **Read-only by design.** Ghost Map only sends EtherNet/IP *ListIdentity* (the same unconnected query RSLinx/FactoryTalk Linx browsing uses), SNMP GET/GETBULK, and TCP connect checks. It has no write, set, or configuration code paths. See [docs/OT-SAFETY.md](docs/OT-SAFETY.md).

## Quick start

```bash
pip install -e .              # or: pip install ghostmap-*.whl on an offline laptop

ghostmap web --demo           # open http://127.0.0.1:8470 and explore a simulated machine cell
```

A real machine network:

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

`ghostmap web` serves on `127.0.0.1:8470` by default. It has these tabs:

- **Overview**: counts and all findings, with a hint for each.
- **Devices**: a sortable, filterable inventory. Click a device for its full CIP identity, decoded status word, related findings, and a one-click probe.
- **Switches**: a port faceplate coloured by health, a port table, and per-port detail (counters, neighbours, MACs mapped to devices).
- **Compare**: the diff of any two scans.
- **Probe**: a single-device check.

**Export CSV** downloads the device inventory.

Scans are stored as JSON in `~/.ghostmap/scans` (override with `--data-dir` or `GHOSTMAP_DATA`). SNMP credentials are never written to disk.

## Preparing a Stratix switch

Ghost Map needs read-only SNMP. These are example IOS settings (Stratix 5400/5410/5700, or the equivalent in Device Manager):

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

IOS exposes the MAC address table per VLAN, so Ghost Map reads the per-VLAN tables as well. It uses `community@<vlan>` indexing for v2c and the `vlan-<id>` context for v3, which is why the v3 group above includes the `vlan-` context. If the switch supports Q-BRIDGE-MIB, one walk is enough.

## Firmware baseline

Pass `--baseline file.json` to flag devices whose firmware differs from the machine standard. See [docs/baseline.example.json](docs/baseline.example.json). Rules match on `product_name`, or on `vendor_id` + `product_code`.

## Extending vendor names

The built-in CIP vendor table covers common automation vendors. To add the full ODVA vendor list, drop `{"<id>": "<name>", ...}` into `ghostmap/data/vendors.json` or point `GHOSTMAP_VENDORS` at such a file.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The switch collector, correlation, and diagnostics all run against an in-memory SNMP agent (`FakeSnmpClient`) built from the simulated machine in `ghostmap/sim/machine.py`, so features can be developed without hardware. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status and roadmap

v0.1 has been tested against the built-in simulators and a real SNMP agent (snmpsim) serving the simulated Stratix. **It has not yet been validated against physical Stratix or Logix hardware.** The first site test should start with `discover` and `switch` on one device.

Planned:
- CIP explicit messaging reads (Ethernet Link object: negotiated speed/duplex and media counters from the *device* side; TCP/IP object: DHCP/BOOTP mode, hostname, gateway)
- Logix backplane browse (modules, slots, firmware in each chassis)
- Stratix specifics: DLR ring status, Device Manager/CIP port-to-device mappings, IOS-XE (Stratix 5800) MIB differences
- Topology diagram view, PDF/Excel commissioning report
- Windows single-file build (PyInstaller) for locked-down engineering laptops

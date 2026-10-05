# Architecture

```
ghostmap/
  protocols/   wire formats: EtherNet/IP ListIdentity, CIP tables, SNMP client + OIDs
  collectors/  talk to the network: discovery sweep, ARP cache, Stratix/IOS switch collector
  analysis/    pure functions: topology correlation, diagnostics rules, scan diff
  scanner.py   orchestrates a scan -> ScanResult
  store.py     JSON scan history + CSV export
  web/         FastAPI app + static single-page UI (no external assets)
  sim/         simulated machine cell, ListIdentity responder, demo data
  cli.py       argparse CLI
  models.py    dataclasses shared by everything (+ JSON (de)serialisation)
```

## Scan pipeline

1. **Discovery** (`collectors/discovery.py`): paced unicast ListIdentity to every target host, plus optional broadcasts. Replies are kept raw (`DiscoveredIdentity`) so that two devices answering from one IP can be detected.
2. **Switches** (`collectors/stratix.py`): read each switch through the `SnmpClient` protocol. Every section (system, entity, interfaces, ethernet, vlans, mac-table, arp, lldp, cdp) is best-effort. A failed section is recorded in `SwitchInfo.errors` and the remaining sections still run.
3. **Correlation** (`analysis/topology.py`): IP to MAC (switch ARP tables plus the scanner's own ARP cache), then MAC to switch port (FDB). A MAC appears on every switch between the scanner and the device, so its location is taken as the non-uplink port with the fewest MACs. Uplinks are detected from CDP/LLDP neighbours.
4. **Diagnostics** (`analysis/diagnostics.py`): stateless rules that produce `Finding(severity, code, target, message, hint)`.
5. **Store** (`store.py`): `ScanResult` is saved as JSON. `analysis/diff.py` compares two scans by stable device key (CIP vendor + serial, then MAC, then IP).

## Adding things

- **A new diagnostic rule**: add it to `run_diagnostics` with a dotted `code` (e.g. `port.foo`) and a hint that tells the engineer what to do. Add a case to the simulated machine and a test.
- **A new switch field**: add the OID to `protocols/mibs.py`, collect it in a section in `stratix.py`, add a field to `Port` or `SwitchInfo`, and add the value to `sim/machine.py:switch_oids` so the demo and tests exercise it.
- **A new protocol** (e.g. CIP explicit messaging, PROFINET DCP, Modbus identification): put the wire format in `protocols/`, the network I/O in `collectors/`, then hook it into `scanner.run_scan`.

## Testing

`FakeSnmpClient` and the ListIdentity responder let the whole pipeline run with no hardware. `build_demo_scan()` runs the real collector, correlation and diagnostics against the simulated "Packaging Line 1" cell. It is used by the tests and by `ghostmap web --demo`.

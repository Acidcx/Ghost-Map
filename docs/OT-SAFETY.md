# OT safety notes

Ghost Map runs on live control networks, so it is deliberately conservative.

## What it sends

| Traffic | Purpose | Notes |
|---|---|---|
| EtherNet/IP `ListIdentity` (UDP 44818) | Discovery, identity, firmware, status | Unconnected and session-less. It is the same request RSLinx / FactoryTalk Linx browsing sends, and it opens no I/O or explicit connection. |
| SNMP GET / GETBULK (UDP 161) | Switch inventory, ports, MAC/ARP tables, neighbours | Read-only. The code has no `set` method. |
| TCP connect (probe only) | Port reachability check | Connects and closes immediately; no payload is sent. Runs only when you probe a single device. |
| OS ARP | IP to MAC mapping | A side effect of contacting hosts on the local subnet. Ghost Map reads the OS ARP cache afterwards. |

It never sends CIP writes, resets, firmware updates, SNMP SET, or configuration changes.

## Load

- The discovery rate is limited (default **200 packets/s**, set with `--rate`). On networks with old or fragile devices, use `--rate 20`.
- Each target gets exactly one request; there are no retries.
- Ranges larger than 4096 hosts are refused unless the code limit is raised.
- SNMP walks are sequential per switch, use GETBULK with 25 repetitions, have a 2 s timeout, and retry once.

## Recommendations

- Get permission from the site or asset owner before scanning, and follow the site MOC process.
- Avoid scanning during critical operations (first runs, batch phases, safety-system testing).
- Prefer SNMPv3 with authPriv, plus a switch ACL that allows only the engineering laptop.
- The web UI binds to `127.0.0.1`. If you expose it with `--host 0.0.0.0`, anyone who can reach the port can start scans.
- Scan files contain the network inventory (IPs, MACs, serials, firmware). Treat them as sensitive site documentation.

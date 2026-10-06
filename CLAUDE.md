# Ghost Map: notes for Claude Code sessions

Ghost Map is a read-only OT network mapper and machine health tool for Rockwell / Stratix machine networks. The plan and the decisions so far are in `ROADMAP.md`. Read it before starting feature work, and tick its checkboxes when items are done.

## Ground rules

- **Read-only device access, always.** Only EtherNet/IP ListIdentity, SNMP GET/GETBULK, TCP connect checks and (planned) Logix tag *reads*. No write, set, reset or configuration code paths anywhere. See `docs/OT-SAFETY.md`.
- **Works offline.** The web UI must not load anything from CDNs or the internet. Bundle any data the app needs (like `ghostmap/data/oui.tsv.gz`).
- **Windows first.** The target is Windows 11 LTSC HMIs and engineering laptops, through the single-file `GhostMap.exe`. Linux and macOS should keep working. Python 3.10+.
- **Never commit real plant data**: scan results, L5X exports, IPs or serials from a customer site. Use the simulated machine in `ghostmap/sim/machine.py` for demos and tests.

## Commands

```bash
pip install -e ".[dev]"            # dev install
pytest -q                          # all tests (fast, no hardware needed)
python -m pyflakes ghostmap tests  # lint
python -m ghostmap web --demo      # UI with the simulated machine at http://127.0.0.1:8470
python -m ghostmap simulate        # fake EtherNet/IP devices on 127.0.10.0/24 (Linux)
pip install pyinstaller && python packaging/build_exe.py   # dist/GhostMap.exe
```

CI runs the tests on Linux and Windows (Python 3.10 and 3.12) and builds and smoke-tests `GhostMap.exe` on every push.

## Conventions

- Layout: `protocols/` = wire formats, `collectors/` = network I/O, `analysis/` = pure functions (correlation, diagnostics, diff). See `docs/ARCHITECTURE.md`.
- When adding a switch or device field, also add it to the simulated machine (`sim/machine.py`) so the demo and tests exercise it.
- Diagnostic findings need a dotted `code` and a `hint` that tells the engineer what to do.
- Real-world behaviour found on site (e.g. RSLinx PCs answering as CIP vendor 77) gets a regression test that replays it.

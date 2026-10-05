"""Entry point for the PyInstaller-built GhostMap.exe.

Double-clicking the exe starts the web UI and opens the browser. Any
arguments are passed to the normal CLI, e.g. ``GhostMap.exe discover 192.168.1.0/24``.
"""

import sys
import traceback

from ghostmap.cli import main

if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code
    except KeyboardInterrupt:
        code = 0
    except Exception:
        traceback.print_exc()
        code = 1
    # Keep the console open on errors when launched by double-click, so the message can be read.
    if code not in (0, None) and len(sys.argv) == 1:
        input("\nGhost Map stopped with an error. Press Enter to close...")
    sys.exit(code)

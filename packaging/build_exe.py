"""Build a single-file Ghost Map executable with PyInstaller.

    pip install . pyinstaller
    python packaging/build_exe.py

Output: dist/GhostMap.exe on Windows (dist/GhostMap elsewhere).
"""

import os
import subprocess
import time
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent


def git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip() or None
    except OSError:
        return None


# Build info shown next to the version in the UI and in the debug bundle, so a test report names the exact build.
build = {"commit": (os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD") or "")[:10] or None,
         "branch": os.environ.get("GITHUB_REF_NAME") or git("rev-parse", "--abbrev-ref", "HEAD"),
         "run": os.environ.get("GITHUB_RUN_NUMBER"), "built": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
(ROOT / "ghostmap" / "_build.py").write_text(f"BUILD = {build!r}\n", encoding="utf-8")

PyInstaller.__main__.run([
    str(ROOT / "packaging" / "ghostmap_exe.py"),
    "--onefile",
    "--console",  # keep the console: it shows the URL and is how you stop the server
    "--name", "GhostMap",
    "--noconfirm",
    "--clean",
    "--paths", str(ROOT),
    "--collect-submodules", "ghostmap",  # the CLI imports most modules lazily
    "--add-data", f"{ROOT / 'ghostmap' / 'web' / 'static'}{os.pathsep}ghostmap/web/static",
    "--add-data", f"{ROOT / 'ghostmap' / 'data'}{os.pathsep}ghostmap/data",
    "--collect-all", "pysnmp",  # MIB modules are loaded dynamically
    "--collect-submodules", "uvicorn",
    "--collect-submodules", "asyncua",  # OPC UA tag browser; some modules are imported dynamically
    "--collect-data", "asyncua",
    "--collect-submodules", "pytds",  # TSC SQL link
    "--hidden-import", "OpenSSL.SSL",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
])

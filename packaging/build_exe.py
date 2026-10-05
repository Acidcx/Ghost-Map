"""Build a single-file Ghost Map executable with PyInstaller.

    pip install . pyinstaller
    python packaging/build_exe.py

Output: dist/GhostMap.exe on Windows (dist/GhostMap elsewhere).
"""

import os
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent

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
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
])

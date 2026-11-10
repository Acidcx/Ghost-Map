"""Build a single-file Ghost Map executable with PyInstaller.

    pip install -c packaging/exe-constraints.txt . pyinstaller   # the pinned versions, as the CI build does
    python packaging/build_exe.py            # single file: dist/GhostMap.exe (dist/GhostMap elsewhere)
    python packaging/build_exe.py --onedir   # unpacked: dist/onedir/GhostMap/GhostMap.exe + _internal, for Setup

The unpacked build is what GhostMap-Setup.exe (packaging/installer.iss) installs: it starts without unpacking
itself to %TEMP% first, so antivirus checks its files once instead of on every start.
"""

import os
import sys
import subprocess
import tempfile
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

# Windows version information (Properties > Details): an exe without it is one more reason for antivirus to
# distrust an unsigned program, and it tells IT what the file is.
from ghostmap import __version__  # noqa: E402

nums = tuple(int(x) for x in __version__.split(".")[:3]) + (int(build["run"] or 0) % 65536,)
product_version = __version__ + (f" ({build['commit']})" if build["commit"] else "")
version_file = Path(tempfile.mkdtemp(prefix="ghostmap-build-")) / "version_info.txt"  # --clean empties build/
version_file.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('ProductName', 'Ghost Map'),
      StringStruct('FileDescription', 'Ghost Map - read-only OT network mapper and machine health monitor'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('ProductVersion', '{product_version}'),
      StringStruct('InternalName', 'GhostMap'),
      StringStruct('OriginalFilename', 'GhostMap.exe'),
      StringStruct('LegalCopyright', 'Proprietary'),
      StringStruct('Comments', 'Read-only: EtherNet/IP ListIdentity, SNMP GET, OPC UA reads. Source: github.com/Acidcx/Ghost-Map')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")

onedir = "--onedir" in sys.argv[1:]
out = ROOT / "dist" / "onedir" if onedir else ROOT / "dist"
work = ROOT / "build" / "onedir" if onedir else ROOT / "build"

PyInstaller.__main__.run([
    str(ROOT / "packaging" / "ghostmap_exe.py"),
    *(["--onedir", "--contents-directory", "_internal"] if onedir else ["--onefile"]),
    "--console",  # keep the console: it shows the URL and is how you stop the server
    "--name", "GhostMap",
    "--noconfirm",
    "--noupx",  # UPX-packed exes are flagged by antivirus far more often
    "--version-file", str(version_file),
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
    "--distpath", str(out),
    "--workpath", str(work),
    "--specpath", str(work),
])

"""Ghost Map - read-only OT network mapper and commissioning/debug tool."""

__version__ = "0.3.0"

try:  # written by packaging/build_exe.py into the exe; absent in a source checkout
    from ghostmap._build import BUILD  # type: ignore
except ImportError:
    BUILD = {}

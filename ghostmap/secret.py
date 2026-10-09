"""Keep a saved password unreadable in the data folder.

On Windows the password is sealed with DPAPI for the machine (CryptProtectData with the local-machine
flag), so a copy of ``tsc.json`` is useless on another PC, and Ghost Map can still read it when it later runs
as a service under another account. Elsewhere it is mixed with a random key kept in a file only the current
user can read; that stops casual reading of the config, not someone who can read both files.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from pathlib import Path

_ENTROPY = b"GhostMap secret v1"


def protect(text: str, root: Path) -> str:
    if not text:
        return ""
    data = text.encode("utf-8")
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(data, protect=True)).decode()
    return "key:" + base64.b64encode(_xor(data, _key(root))).decode()


def unprotect(blob: str, root: Path) -> str:
    if not blob:
        return ""
    kind, _, b64 = blob.partition(":")
    raw = base64.b64decode(b64)
    if kind == "dpapi":
        if sys.platform != "win32":
            raise ValueError("this password was saved on Windows; enter it again")
        return _dpapi(raw, protect=False).decode("utf-8")
    if kind == "key":
        return _xor(raw, _key(root)).decode("utf-8")
    raise ValueError("unknown saved password format; enter it again")


def _key(root: Path) -> bytes:
    p = Path(root) / ".secret.key"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    return p.read_bytes()


def _xor(data: bytes, key: bytes) -> bytes:
    stream = b""
    counter = 0
    while len(stream) < len(data):
        stream += hashlib.sha256(key + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(data, stream))


def _dpapi(data: bytes, protect: bool) -> bytes:  # pragma: no cover - Windows only (run by the Windows CI job)
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> Blob:
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, ent, out = blob(data), blob(_ENTROPY), Blob()
    CRYPTPROTECT_UI_FORBIDDEN, CRYPTPROTECT_LOCAL_MACHINE = 0x1, 0x4
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(src), "Ghost Map", ctypes.byref(ent), None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise ValueError("Windows could not unlock the saved password (copied from another PC?); enter it again")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)

"""Web UI access control: client allow-list, user accounts, sessions, audit log.

Once Ghost Map is reachable through the IXON IXrouter it is an outward-facing
service on an OT asset, so:

- only loopback and allow-listed client addresses may connect at all;
- when any user account exists, every API call needs a login, and actions
  that send traffic or delete data need the ``admin`` role;
- logins and actions are appended to ``audit.log`` in the data directory.

Accounts live in ``users.json`` in the data directory with PBKDF2-SHA256
password hashes. Sessions are held in memory, so restarting the service logs
everyone out. No third-party dependencies.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

ROLES = ("viewer", "admin")
PBKDF2_ITERATIONS = 310_000
MIN_PASSWORD_LEN = 10
SESSION_TTL_S = 12 * 3600
MAX_FAILURES = 5
LOCKOUT_S = 300

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def parse_allow(specs: Iterable[str]) -> list[ipaddress._BaseNetwork]:
    """Parse ``--allow`` values (single IPs or CIDR subnets)."""
    nets = []
    for spec in specs:
        try:
            nets.append(ipaddress.ip_network(spec.strip(), strict=False))
        except ValueError:
            raise ValueError(f"bad --allow value {spec!r}: expected an IP or subnet like 192.168.1.1 or 10.0.0.0/24")
    return nets


def client_allowed(host: Optional[str], allow: list) -> bool:
    """Loopback is always allowed; anything else must be inside an allow-listed network."""
    try:
        addr = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return True
    return any(addr.version == net.version and addr in net for net in allow)


def _hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


class UserStore:
    """Accounts in ``<data_dir>/users.json``."""

    def __init__(self, root: Path):
        self.path = Path(root) / "users.json"

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, users: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(users, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def has_users(self) -> bool:
        return bool(self._load())

    def list(self) -> list[dict]:
        return [{"name": n, "role": u["role"]} for n, u in sorted(self._load().items())]

    def set(self, name: str, password: str, role: str) -> None:
        if not _NAME_RE.match(name):
            raise ValueError("user names are 1-32 letters, digits, '.', '_' or '-'")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"passwords need at least {MIN_PASSWORD_LEN} characters")
        salt = os.urandom(16)
        users = self._load()
        users[name] = {"role": role, "salt": salt.hex(), "iterations": PBKDF2_ITERATIONS,
                       "hash": _hash(password, salt, PBKDF2_ITERATIONS).hex()}
        self._save(users)

    def remove(self, name: str) -> None:
        users = self._load()
        if name not in users:
            raise KeyError(name)
        del users[name]
        self._save(users)

    def verify(self, name: str, password: str) -> Optional[str]:
        """Return the user's role if the password is right, else None."""
        u = self._load().get(name)
        if u is None:
            _hash(password, b"\0" * 16, PBKDF2_ITERATIONS)  # same cost whether or not the user exists
            return None
        got = _hash(password, bytes.fromhex(u["salt"]), u["iterations"])
        return u["role"] if hmac.compare_digest(got, bytes.fromhex(u["hash"])) else None


@dataclass
class Session:
    user: str
    role: str
    expires: float


class Sessions:
    def __init__(self):
        self._by_token: dict[str, Session] = {}

    def create(self, user: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        self._by_token[token] = Session(user, role, time.time() + SESSION_TTL_S)
        return token

    def get(self, token: Optional[str]) -> Optional[Session]:
        s = self._by_token.get(token or "")
        if s and s.expires < time.time():
            del self._by_token[token]
            return None
        return s

    def drop(self, token: Optional[str]) -> None:
        self._by_token.pop(token or "", None)


class Lockout:
    """Lock a client address or user name out after repeated failed logins."""

    def __init__(self):
        self._fails: dict[str, list[float]] = {}

    def locked(self, *keys: str) -> bool:
        now = time.time()
        for k in keys:
            recent = [t for t in self._fails.get(k, []) if now - t < LOCKOUT_S]
            self._fails[k] = recent
            if len(recent) >= MAX_FAILURES:
                return True
        return False

    def fail(self, *keys: str) -> None:
        for k in keys:
            self._fails.setdefault(k, []).append(time.time())

    def clear(self, *keys: str) -> None:
        for k in keys:
            self._fails.pop(k, None)


class AuditLog:
    def __init__(self, root: Path):
        self.path = Path(root) / "audit.log"

    def write(self, client: Optional[str], user: Optional[str], action: str, detail: str = "") -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = "\t".join((ts, client or "-", user or "-", action, detail.replace("\n", " ")))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

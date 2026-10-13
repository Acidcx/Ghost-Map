"""The OPC UA server certificates Ghost Map has seen, pinned per gateway address (like SSH's known_hosts).

The first certificate a gateway shows is trusted and kept. From then on a different certificate at the same
address means the gateway was reinstalled, its certificate renewed, or something else is answering there:

- On a signed or encrypted session the connection is refused until an admin accepts the new certificate
  (Tags (OPC UA) > Gateway certificates). Without that check, security on the session proves nothing.
- On a Security None session (how dashboards read, and how FT Linx Gateway is usually set up) nothing is
  signed anyway, so Ghost Map keeps reading, logs it and shows a warning until an admin accepts it.

Kept in ``<data_dir>/opcua-trust.json``. Only certificates are stored, never keys.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

TRUST_FILE = "opcua-trust.json"
FORMAT = 1
log = logging.getLogger("ghostmap.ua_trust")


class CertificateChanged(Exception):
    """A signed or encrypted session to a gateway whose certificate is not the one pinned for it."""


def server_key(url: str) -> str:
    """Pins are per address: host and port of the endpoint."""
    u = urlparse(str(url).strip())
    host = (u.hostname or "").lower()
    return f"{host}:{u.port or 4840}" if host else str(url).strip().lower()


def cert_info(der: bytes) -> dict:
    """Fingerprints and the readable parts of a DER certificate (whatever of it parses)."""
    info = {"sha256": hashlib.sha256(der).hexdigest(), "sha1": hashlib.sha1(der).hexdigest(),
            "subject": "", "issuer": "", "not_after": None, "application_uri": ""}
    try:
        from cryptography import x509

        c = x509.load_der_x509_certificate(der)
        info["subject"] = c.subject.rfc4514_string()
        info["issuer"] = c.issuer.rfc4514_string()
        after = getattr(c, "not_valid_after_utc", None) or c.not_valid_after.replace(tzinfo=_dt.timezone.utc)
        info["not_after"] = after.timestamp()
        try:
            san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            uris = san.get_values_for_type(x509.UniformResourceIdentifier)
            info["application_uri"] = uris[0] if uris else ""
        except x509.ExtensionNotFound:
            pass
    except Exception:  # an odd certificate is still pinned by its fingerprint
        pass
    return info


class CertTrust:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / TRUST_FILE
        self._lock = threading.Lock()
        self.servers: dict[str, dict] = {}   # address -> the pinned certificate
        self.changed: dict[str, dict] = {}   # address -> a different certificate seen there, not yet accepted
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("can't read %s (%s); starting with no pinned certificates", self.path, exc)
            return
        self.servers = dict(data.get("servers") or {})
        self.changed = dict(data.get("changed") or {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.stem}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps({"format": FORMAT, "servers": self.servers, "changed": self.changed}, indent=1),
                       encoding="utf-8")
        for i in range(40):  # Windows refuses the replace while a virus scanner has the file open
            try:
                tmp.replace(self.path)
                return
            except PermissionError:
                if i == 39:
                    raise
                time.sleep(0.05)

    def check(self, url: str, der: bytes, secure: bool) -> Optional[str]:
        """Called with the certificate a gateway sent while a session is being created.

        Pins it if the address is new. Returns None when it is the pinned one, a warning when it differs on a
        Security None session, and raises CertificateChanged when it differs on a secure session.
        """
        key, now = server_key(url), time.time()
        info = cert_info(der)
        with self._lock:
            pinned = self.servers.get(key)
            if pinned is None:
                self.servers[key] = {**info, "url": url, "pinned_at": now, "pinned_by": "first connection"}
                self.changed.pop(key, None)
                self._save()
                log.info("pinned the certificate of %s (SHA-256 %s)", key, info["sha256"][:16])
                return None
            if pinned["sha256"] == info["sha256"]:
                return None
            seen = self.changed.get(key)
            if not seen or seen["sha256"] != info["sha256"]:
                self.changed[key] = {**info, "url": url, "first_seen": now}
                self._save()
                log.warning("the certificate of %s changed: pinned SHA-256 %s, now %s", key, pinned["sha256"][:16],
                            info["sha256"][:16])
        msg = (f"The certificate of the OPC UA server at {key} is not the one Ghost Map pinned on "
               f"{_day(pinned.get('pinned_at'))} (SHA-256 now {info['sha256'][:16]}..., was "
               f"{pinned['sha256'][:16]}...). If the gateway was reinstalled or its certificate renewed, "
               "an admin can accept the new one in Tags (OPC UA) > Gateway certificates.")
        if secure:
            raise CertificateChanged(msg)
        return msg

    def warning(self, url: Optional[str]) -> Optional[dict]:
        """The unaccepted certificate change at this address, if there is one."""
        if not url:
            return None
        return self.changed.get(server_key(url))

    def accept(self, key: str, sha256: str, who: str = "") -> dict:
        """Trust the changed certificate at ``key`` from now on (the one with this fingerprint only)."""
        with self._lock:
            seen = self.changed.get(key)
            if not seen or seen["sha256"] != sha256:
                raise KeyError(key)
            self.servers[key] = {**self.changed.pop(key), "pinned_at": time.time(),
                                 "pinned_by": f"accepted by {who or 'an admin'}"}
            self._save()
            return self.servers[key]

    def forget(self, key: str) -> None:
        """Drop the pin: the next certificate seen at this address is trusted as if for the first time."""
        with self._lock:
            if key not in self.servers and key not in self.changed:
                raise KeyError(key)
            self.servers.pop(key, None)
            self.changed.pop(key, None)
            self._save()

    def list(self) -> list[dict]:
        out = []
        for key in sorted(set(self.servers) | set(self.changed)):
            out.append({"key": key, "pinned": self.servers.get(key), "changed": self.changed.get(key)})
        return out


def _day(t) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else "an earlier connection"

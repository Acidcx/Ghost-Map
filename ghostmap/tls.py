"""HTTPS for the web UI (``web --https``, ``service install --https``).

By default Ghost Map makes its own self-signed certificate on first start, in ``<data_dir>/tls``: the PC's name,
``localhost``, 127.0.0.1 and the PC's own IPv4 addresses, valid ``VALID_DAYS``. It is made again on a start
when it has less than ``RENEW_DAYS`` left or the PC was renamed. Browsers warn about a self-signed certificate
until it is trusted (import ``ghostmap.crt`` into the browsing PC's Trusted Root store, or check the SHA-256
fingerprint Ghost Map prints against the one the browser shows). A certificate from the plant's own CA can be
used instead with ``--cert`` and ``--key``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
import socket
from pathlib import Path
from typing import Optional

TLS_DIR = "tls"
CERT_NAME, KEY_NAME = "ghostmap.crt", "ghostmap.key"
VALID_DAYS = 5 * 365
RENEW_DAYS = 30


def host_names() -> tuple[list[str], list[str]]:
    """(DNS names, IP addresses) this PC answers to, for the certificate's subject alternative names."""
    host = socket.gethostname()
    names = [n for n in dict.fromkeys([host, host.split(".")[0], "localhost"]) if n and n.isascii()]
    try:
        fqdn = socket.getfqdn()
        if fqdn and "." in fqdn and fqdn.isascii() and fqdn not in names and not fqdn.endswith(".arpa"):
            names.append(fqdn)
    except OSError:
        pass
    ips = ["127.0.0.1", "::1"]
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("169.254."):
                ips.append(ip)
    except OSError:
        pass
    return names, ips


def make_self_signed(names: list[str], ips: list[str], days: int = VALID_DAYS) -> tuple[bytes, bytes]:
    """A self-signed server certificate and its private key, both PEM."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ghost Map (self-signed)")])
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def describe(cert_pem: bytes) -> dict:
    """SHA-256 fingerprint (as browsers show it), expiry and names of a PEM certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(Encoding.DER)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)] + [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=dt.timezone.utc)
    return {"sha256": ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()), "not_after": not_after.timestamp(),
            "subject": cert.subject.rfc4514_string(), "self_signed": cert.issuer == cert.subject, "names": names}


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def ensure(data_dir, cert: Optional[str] = None, key: Optional[str] = None) -> dict:
    """The certificate and key to serve HTTPS with: ``cert``/``key`` when given, else Ghost Map's own (made or
    renewed as needed). Returns ``describe()`` plus the ``cert`` and ``key`` paths and ``made`` (new this time)."""
    if cert or key:
        if not (cert and key):
            raise ValueError("--cert and --key go together")
        for p in (cert, key):
            if not Path(p).is_file():
                raise ValueError(f"{p} not found")
        return {**describe(Path(cert).read_bytes()), "cert": str(cert), "key": str(key), "made": False}
    d = Path(data_dir) / TLS_DIR
    c, k = d / CERT_NAME, d / KEY_NAME
    names, ips = host_names()
    if c.is_file() and k.is_file():
        try:
            info = describe(c.read_bytes())
            fresh = info["not_after"] - dt.datetime.now(dt.timezone.utc).timestamp() > RENEW_DAYS * 86400
            if fresh and names[0] in info["names"]:
                return {**info, "cert": str(c), "key": str(k), "made": False}
        except ValueError:
            pass  # unreadable: make a new one
    d.mkdir(parents=True, exist_ok=True)
    cert_pem, key_pem = make_self_signed(names, ips)
    _write_private(k, key_pem)
    tmp = c.with_name(f"{c.name}.{os.getpid()}.tmp")
    tmp.write_bytes(cert_pem)
    os.replace(tmp, c)
    return {**describe(cert_pem), "cert": str(c), "key": str(k), "made": True}

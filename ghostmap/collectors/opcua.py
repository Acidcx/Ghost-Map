"""Read-only OPC UA client for the Tag Browser (e.g. FactoryTalk Linx Gateway).

Only browse, read and GetEndpoints are implemented. There is deliberately no
write, call or node-management code here: Ghost Map never changes a value on
a controller. Values are converted to JSON-friendly types for the web UI.

FactoryTalk Linx Gateway serves OPC UA on ``opc.tcp://<host>:4990`` by
default, with Security None and anonymous login available.
"""

from __future__ import annotations

import base64
import datetime as _dt
import enum
import hashlib
import socket
from pathlib import Path
from typing import Any, Optional

from asyncua import Client, ua
from asyncua.crypto import security_policies

DEFAULT_PORT = 4990
MAX_READ = 200            # nodes per read request from the UI
MAX_EXPORT_NODES = 20000  # nodes visited per tag-list export
MAX_EXPORT_DEPTH = 16
MAX_ARRAY_PREVIEW = 64

SECURITY_POLICIES = {
    "None": None,
    "Basic256Sha256": security_policies.SecurityPolicyBasic256Sha256,
    "Aes128Sha256RsaOaep": security_policies.SecurityPolicyAes128Sha256RsaOaep,
    "Aes256Sha256RsaPss": security_policies.SecurityPolicyAes256Sha256RsaPss,
}
SECURITY_MODES = {
    "Sign": ua.MessageSecurityMode.Sign,
    "SignAndEncrypt": ua.MessageSecurityMode.SignAndEncrypt,
}


def normalize_endpoint(url: str) -> str:
    """Accept a bare IP/host ("192.168.1.10" or "hmi01:4990") as well as a full opc.tcp:// URL."""
    url = url.strip()
    if not url:
        raise ValueError("enter an endpoint, e.g. opc.tcp://127.0.0.1:4990")
    if "://" not in url:
        url = f"opc.tcp://{url}"
    if not url.startswith("opc.tcp://"):
        raise ValueError("only opc.tcp:// endpoints are supported")
    hostport = url[len("opc.tcp://"):].split("/", 1)[0]
    if not hostport:
        raise ValueError("endpoint has no host")
    if ":" not in hostport.rsplit("]", 1)[-1]:
        url = url.replace(hostport, f"{hostport}:{DEFAULT_PORT}", 1)
    return url


def jsonable(v: Any, depth: int = 0) -> Any:
    """Turn an OPC UA value into something json.dumps can handle."""
    if depth > 4:
        return str(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, enum.Enum):
        return v.name
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return base64.b16encode(bytes(v[:256])).decode()
    if isinstance(v, ua.NodeId):
        return v.to_string()
    if isinstance(v, ua.QualifiedName):
        return v.to_string()
    if isinstance(v, ua.LocalizedText):
        return v.Text
    if isinstance(v, ua.Variant):
        return jsonable(v.Value, depth + 1)
    if isinstance(v, (list, tuple)):
        out = [jsonable(x, depth + 1) for x in v[:MAX_ARRAY_PREVIEW]]
        if len(v) > MAX_ARRAY_PREVIEW:
            out.append(f"... {len(v) - MAX_ARRAY_PREVIEW} more")
        return out
    if hasattr(v, "__dataclass_fields__"):  # structures / extension objects
        return {k: jsonable(getattr(v, k), depth + 1) for k in v.__dataclass_fields__}
    return str(v)


def _status(dv: ua.DataValue) -> str:
    code = dv.StatusCode
    return code.name if code is not None else "Good"


def _endpoint_info(e: ua.EndpointDescription) -> dict:
    return {
        "url": e.EndpointUrl,
        "security_policy": e.SecurityPolicyUri.rsplit("#", 1)[-1],
        "security_mode": e.SecurityMode.name.rstrip("_"),
        "user_tokens": sorted({t.TokenType.name for t in e.UserIdentityTokens}),
        "server": e.Server.ApplicationName.Text if e.Server and e.Server.ApplicationName else "",
        "level": e.SecurityLevel,
    }


async def get_endpoints(url: str, timeout: float = 5.0) -> list[dict]:
    """Discovery only (GetEndpoints); no session is created."""
    client = Client(normalize_endpoint(url), timeout=timeout)
    eps = await client.connect_and_get_server_endpoints()
    return [_endpoint_info(e) for e in eps]


class UaBrowser:
    """One read-only OPC UA session."""

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = normalize_endpoint(url)
        self.client = Client(self.url, timeout=timeout)
        self.server_cert_sha1: Optional[str] = None

    async def connect(self, security: str = "None", mode: str = "SignAndEncrypt", username: str = "",
                      password: str = "", pki_dir: Optional[Path] = None) -> dict:
        if security not in SECURITY_POLICIES:
            raise ValueError(f"security must be one of {', '.join(SECURITY_POLICIES)}")
        if security != "None":
            if mode not in SECURITY_MODES:
                raise ValueError("mode must be Sign or SignAndEncrypt")
            key, cert = await ensure_client_certificate(pki_dir or Path.home() / ".ghostmap" / "pki")
            self.client.application_uri = "urn:ghostmap:client"
            await self.client.set_security(SECURITY_POLICIES[security], str(cert), str(key),
                                           mode=SECURITY_MODES[mode])
        if username:
            self.client.set_user(username)
            self.client.set_password(password)
        await self.client.connect()
        info = {"url": self.url, "security": security if security != "None" else "None",
                "mode": mode if security != "None" else "None", "user": username or "anonymous"}
        try:
            ns = await self.client.get_namespace_array()
            info["namespaces"] = ns
        except Exception:  # some servers restrict this; not fatal
            info["namespaces"] = []
        try:
            build = await self.client.nodes.server.get_child(["0:ServerStatus", "0:BuildInfo"])
            b = await build.read_value()
            info["server"] = f"{b.ManufacturerName} {b.ProductName} {b.SoftwareVersion}".strip()
        except Exception:
            info["server"] = ""
        cert = getattr(self.client.security_policy, "peer_certificate", None)
        if cert:
            self.server_cert_sha1 = hashlib.sha1(cert).hexdigest()
            info["server_cert_sha1"] = self.server_cert_sha1
        return info

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def browse(self, node_id: Optional[str] = None) -> list[dict]:
        node = self.client.get_node(node_id) if node_id else self.client.nodes.objects
        refs = await node.get_references(refs=ua.ObjectIds.HierarchicalReferences,
                                         direction=ua.BrowseDirection.Forward,
                                         nodeclassmask=ua.NodeClass.Object | ua.NodeClass.Variable)
        out = []
        for r in refs:
            out.append({
                "node_id": r.NodeId.to_string(),
                "browse_name": r.BrowseName.to_string(),
                "name": r.DisplayName.Text or r.BrowseName.Name,
                "node_class": r.NodeClass.name,
            })
        out.sort(key=lambda x: x["name"].lower())
        return out

    async def read(self, node_ids: list[str]) -> list[dict]:
        if len(node_ids) > MAX_READ:
            raise ValueError(f"at most {MAX_READ} nodes per read")
        params = ua.ReadParameters()
        for nid in node_ids:
            rv = ua.ReadValueId()
            rv.NodeId = ua.NodeId.from_string(nid)
            rv.AttributeId = ua.AttributeIds.Value
            params.NodesToRead.append(rv)
        results = await self.client.uaclient.read(params) if params.NodesToRead else []
        out = []
        for nid, dv in zip(node_ids, results):
            out.append({
                "node_id": nid,
                "value": jsonable(dv.Value.Value) if dv.Value is not None else None,
                "variant_type": dv.Value.VariantType.name if dv.Value is not None else None,
                "status": _status(dv),
                "source_time": jsonable(dv.SourceTimestamp),
                "server_time": jsonable(dv.ServerTimestamp),
            })
        return out

    async def attributes(self, node_id: str) -> dict:
        node = self.client.get_node(node_id)
        names = ("NodeId", "NodeClass", "BrowseName", "DisplayName", "Description", "DataType",
                 "ValueRank", "ArrayDimensions", "AccessLevel", "UserAccessLevel", "MinimumSamplingInterval")
        dvs = await node.read_attributes([getattr(ua.AttributeIds, n) for n in names])
        attrs: dict[str, Any] = {}
        for n, dv in zip(names, dvs):
            if dv.StatusCode is not None and not dv.StatusCode.is_good():
                continue
            attrs[n] = jsonable(dv.Value.Value)
        if isinstance(attrs.get("NodeClass"), int):
            attrs["NodeClass"] = ua.NodeClass(attrs["NodeClass"]).name
        if attrs.get("NodeClass") == "Variable":
            if "DataType" in attrs:
                try:
                    dt = await self.client.get_node(attrs["DataType"]).read_browse_name()
                    attrs["DataTypeName"] = dt.Name
                except Exception:
                    pass
            for key in ("AccessLevel", "UserAccessLevel"):
                if isinstance(attrs.get(key), int):
                    attrs[key] = _access_text(attrs[key])
            attrs["Value"] = (await self.read([node_id]))[0]
        return attrs

    async def export(self, node_id: Optional[str] = None) -> dict:
        """Walk a subtree and list every variable with its path, data type and current value.

        This is the raw material for machine profiles: compare the tag lists of two
        machines to see where naming drifts.
        """
        start = node_id or self.client.nodes.objects.nodeid.to_string()
        rows: list[dict] = []
        visited = 0
        truncated = False
        stack: list[tuple[str, str, int]] = [(start, "", 0)]
        seen = {start}
        while stack:
            nid, path, depth = stack.pop()
            if visited >= MAX_EXPORT_NODES:
                truncated = True
                break
            visited += 1
            try:
                children = await self.browse(nid)
            except Exception:
                continue
            for c in children:
                child_path = f"{path}/{c['name']}" if path else c["name"]
                if c["node_class"] == "Variable":
                    rows.append({"path": child_path, "node_id": c["node_id"]})
                if depth + 1 < MAX_EXPORT_DEPTH and c["node_id"] not in seen:
                    seen.add(c["node_id"])
                    stack.append((c["node_id"], child_path, depth + 1))
        rows.sort(key=lambda r: r["path"].lower())
        for i in range(0, len(rows), MAX_READ):
            chunk = rows[i:i + MAX_READ]
            for row, val in zip(chunk, await self.read([r["node_id"] for r in chunk])):
                row.update(variant_type=val["variant_type"], value=val["value"], status=val["status"])
        return {"tags": rows, "truncated": truncated}


def _access_text(level: int) -> str:
    bits = [(1, "Read"), (2, "Write"), (4, "HistoryRead"), (8, "HistoryWrite")]
    return ", ".join(name for bit, name in bits if level & bit) or "None"


async def ensure_client_certificate(pki_dir: Path) -> tuple[Path, Path]:
    """Self-signed application certificate for secure endpoints, created once per install."""
    from asyncua.crypto.cert_gen import setup_self_signed_certificate
    from cryptography.x509.oid import ExtendedKeyUsageOID

    pki_dir.mkdir(parents=True, exist_ok=True)
    key, cert = pki_dir / "ghostmap-key.pem", pki_dir / "ghostmap-cert.der"
    if not cert.exists():
        host = socket.gethostname()
        await setup_self_signed_certificate(
            key, cert, "urn:ghostmap:client", host, [ExtendedKeyUsageOID.CLIENT_AUTH],
            {"countryName": "US", "organizationName": "Ghost Map", "commonName": f"Ghost Map @ {host}"})
    return key, cert

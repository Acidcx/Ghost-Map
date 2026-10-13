"""Read-only OPC UA client for the Tag Browser and the machine dashboards (e.g. FactoryTalk Linx Gateway).

Only browse, read, subscriptions (data-change notifications) and GetEndpoints are implemented. There is
deliberately no write, call or node-management code here: Ghost Map never changes a value on a controller.
A subscription only asks the gateway to send values when they change. Values are converted to JSON-friendly
types for the web UI.

FactoryTalk Linx Gateway serves OPC UA on ``opc.tcp://<host>:4990`` by
default, with Security None and anonymous login available.
"""

from __future__ import annotations

import base64
import datetime as _dt
import enum
import hashlib
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from asyncua import Client, ua
from asyncua.crypto import security_policies

DEFAULT_PORT = 4990
MAX_READ = 200            # nodes per read request from the UI
MAX_EXPORT_NODES = 200000  # nodes visited per tag-list export
EXPORT_BATCH = 50          # nodes per Browse request
BROWSE_MAX_REFS = 1000     # children per node per response; the rest come via BrowseNext
MAX_EXPORT_DEPTH = 16
MAX_ARRAY_PREVIEW = 64
SERVER_STATE = "i=2259"    # Server.ServerStatus.State: a one-value read that proves the session is alive
CHANGES_KEPT = 20000       # value changes buffered per subscription for the history (oldest dropped first)
CLOCK_SKEW_S = 5.0         # a source timestamp further than this from Ghost Map's clock is not trusted

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


# Status codes that mean the session or the connection is gone (not a problem with one tag).
_SESSION_LOST = {"BadSessionIdInvalid", "BadSessionClosed", "BadSessionNotActivated", "BadConnectionClosed",
                 "BadSecureChannelClosed", "BadSecureChannelIdInvalid", "BadNotConnected", "BadServerHalted",
                 "BadServerNotConnected", "BadTimeout", "BadCommunicationError", "BadShutdown",
                 "BadTooManySessions", "BadNoCommunication"}


def is_connection_error(exc: BaseException) -> bool:
    """True when ``exc`` means the OPC UA connection should be re-opened (vs. a bad request)."""
    import asyncio

    if isinstance(exc, (ConnectionError, asyncio.TimeoutError, TimeoutError, OSError)):
        return True
    if isinstance(exc, ua.UaStatusCodeError):
        return type(exc).__name__ in _SESSION_LOST or ua.StatusCode(exc.code).name in _SESSION_LOST
    return "closed" in str(exc).lower() or "not connected" in str(exc).lower()


def _status(dv: ua.DataValue) -> str:
    code = dv.StatusCode
    return code.name if code is not None else "Good"


def _epoch(t) -> Optional[float]:
    if not isinstance(t, _dt.datetime):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)  # OPC UA timestamps are UTC
    try:
        return t.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


class SubscriptionRefused(Exception):
    """The gateway won't give us a subscription (too many, not supported, ...), but the session is fine."""


class UaFeed:
    """The latest value of every subscribed tag, kept up to date by the gateway's data-change notifications.

    ``values``/``bad`` look exactly like a full read. ``changes`` lists every change since it was last drained,
    in arrival order, as ``(time, node_id, value, status)``; the time is the gateway's source timestamp when its
    clock agrees with ours, else when the notification arrived. Only changes after ``seed()`` are kept, so the
    burst of initial values the gateway sends for a new subscription doesn't count as changes.
    """

    def __init__(self, node_ids: list[str]):
        self.ids = list(dict.fromkeys(node_ids))
        self.values: dict[str, Any] = {}
        self.bad: dict[str, str] = {}
        self.changes: deque = deque(maxlen=CHANGES_KEPT)
        self.dropped = 0          # changes lost because nobody drained them in time
        self.notifications = 0
        self.refused: dict[str, str] = {}  # tags the gateway would not monitor, with its status
        self.dead: Optional[str] = None   # why the subscription stopped (set by a status-change notification)
        self.seeded = False
        self.created = time.time()
        self.last_check = self.last_full = 0.0
        self.sub = None
        self._by_node: dict = {}

    # asyncua calls these two from its publish loop
    def datachange_notification(self, node, val, data) -> None:
        nid = self._by_node.get(node.nodeid)
        if nid is None:
            return
        self.notifications += 1
        dv = data.monitored_item.Value
        value = jsonable(dv.Value.Value) if dv is not None and dv.Value is not None else None
        status = _status(dv) if dv is not None else "BadNoData"
        now = time.time()
        src = _epoch(dv.SourceTimestamp) if dv is not None else None
        t = src if src is not None and abs(src - now) <= CLOCK_SKEW_S else now
        self._apply(nid, value, status, t)

    def status_change_notification(self, status) -> None:
        code = getattr(status, "Status", status)
        self.dead = f"subscription ended by the gateway ({getattr(code, 'name', code)})"

    def _apply(self, nid: str, value, status: str, t: float) -> None:
        good = str(status).startswith("Good")
        was_bad = nid in self.bad
        if self.seeded and (self.values.get(nid) != value or was_bad == good):
            if len(self.changes) == self.changes.maxlen:
                self.dropped += 1
            self.changes.append((t, nid, value, status))
        self.values[nid] = value
        if good:
            self.bad.pop(nid, None)
        else:
            self.bad[nid] = status

    def seed(self, rows: list[dict]) -> None:
        """Start from a full read: everything the gateway sends from here on is a change."""
        for r in rows:
            self.values[r["node_id"]] = r["value"]
            if str(r["status"]).startswith("Good"):
                self.bad.pop(r["node_id"], None)
            else:
                self.bad[r["node_id"]] = r["status"]
        self.bad.update(self.refused)
        self.seeded = True

    def reconcile(self, rows: list[dict], t: float) -> int:
        """Fold in a full read (a periodic check that nothing was missed). Returns how many tags differed."""
        n = 0
        for r in rows:
            nid = r["node_id"]
            if nid in self.refused:
                continue
            if self.values.get(nid) != r["value"] or (nid in self.bad) == str(r["status"]).startswith("Good"):
                n += 1
                self._apply(nid, r["value"], r["status"], t)
        return n

    def drain(self) -> list[tuple]:
        out = list(self.changes)
        self.changes.clear()
        return out

    def publish_age(self) -> Optional[float]:
        """Seconds since the gateway last answered a publish (data or keep-alive); None if asyncua doesn't say."""
        last = getattr(self.sub, "last_publish_at", None)
        return time.monotonic() - last if isinstance(last, (int, float)) and last else None

    async def close(self) -> None:
        if self.sub is not None:
            try:
                await self.sub.delete()
            except Exception:
                pass
            self.sub = None


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
        self.cert_warning: Optional[str] = None  # the server's certificate is not the pinned one (Security None)

    async def connect(self, security: str = "None", mode: str = "SignAndEncrypt", username: str = "",
                      password: str = "", pki_dir: Optional[Path] = None, trust=None) -> dict:
        """Open the session. ``trust`` (a ``ua_trust.CertTrust``) checks the server's certificate against the
        one pinned for this address while the session is created; a secure session to a server whose
        certificate changed is refused there (``CertificateChanged``)."""
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
        if trust is not None:
            async def check(cert, _server) -> None:
                from cryptography.hazmat.primitives.serialization import Encoding

                self.cert_warning = trust.check(self.url, cert.public_bytes(Encoding.DER), secure=security != "None")

            self.client.certificate_validator = check
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
            info["server_cert_sha256"] = hashlib.sha256(cert).hexdigest()
        info["cert_warning"] = self.cert_warning
        return info

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:
            pass

    def connected(self) -> bool:
        """False once the socket under the session is closed (no request is sent to find out)."""
        proto = getattr(self.client.uaclient, "protocol", None)
        state = getattr(proto, "state", None)
        return proto is not None and getattr(state, "name", "OPEN") != "CLOSED"

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
        good_ids, out = [], []
        for nid in node_ids:
            try:
                parsed = ua.NodeId.from_string(nid)
            except Exception:
                # One mistyped NodeId (from a hand-edited CSV) must not fail the read for every other tag.
                out.append({"node_id": nid, "value": None, "variant_type": None, "status": "BadNodeIdInvalid",
                            "source_time": None, "server_time": None})
                continue
            rv = ua.ReadValueId()
            rv.NodeId = parsed
            rv.AttributeId = ua.AttributeIds.Value
            params.NodesToRead.append(rv)
            good_ids.append(nid)
        results = await self.client.uaclient.read(params) if params.NodesToRead else []
        for nid, dv in zip(good_ids, results):
            out.append({
                "node_id": nid,
                "value": jsonable(dv.Value.Value) if dv.Value is not None else None,
                "variant_type": dv.Value.VariantType.name if dv.Value is not None else None,
                "status": _status(dv),
                "source_time": jsonable(dv.SourceTimestamp),
                "server_time": jsonable(dv.ServerTimestamp),
            })
        return out

    async def subscribe(self, node_ids: list[str], sample_ms: int = 250, publish_ms: int = 1000,
                        queue: int = 8) -> UaFeed:
        """A data-change subscription on ``node_ids``. Raises SubscriptionRefused when the gateway says no.

        The gateway samples each tag every ``sample_ms`` and sends what changed every ``publish_ms``, queueing up
        to ``queue`` changes per tag in between, so a bit that flicks on and off within one publish still
        arrives as two changes. A keep-alive comes at least every 10 publishes when nothing changes.
        """
        feed = UaFeed(node_ids)
        params = ua.CreateSubscriptionParameters()
        params.RequestedPublishingInterval = float(publish_ms)
        params.RequestedMaxKeepAliveCount = 10
        params.RequestedLifetimeCount = 60
        params.MaxNotificationsPerPublish = 0
        params.PublishingEnabled = True
        params.Priority = 0
        try:
            feed.sub = await self.client.create_subscription(params, feed)
        except Exception as exc:
            if is_connection_error(exc):
                raise
            raise SubscriptionRefused(f"{type(exc).__name__}: {exc}") from exc
        nodes = []
        for nid in feed.ids:
            try:
                parsed = ua.NodeId.from_string(nid)
            except Exception:
                feed.refused[nid] = "BadNodeIdInvalid"
                continue
            feed._by_node[parsed] = nid
            nodes.append((nid, self.client.get_node(parsed)))
        try:
            for i in range(0, len(nodes), MAX_READ):
                chunk = nodes[i:i + MAX_READ]
                handles = await feed.sub.subscribe_data_change([n for _, n in chunk], queuesize=queue,
                                                               sampling_interval=float(sample_ms))
                for (nid, _), h in zip(chunk, handles if isinstance(handles, list) else [handles]):
                    if isinstance(h, ua.StatusCode):
                        feed.refused[nid] = h.name
        except Exception as exc:
            await feed.close()
            if is_connection_error(exc):
                raise
            raise SubscriptionRefused(f"{type(exc).__name__}: {exc}") from exc
        if nodes and len(feed.refused) >= len(feed.ids):
            reasons = sorted(set(feed.refused.values()))
            await feed.close()
            raise SubscriptionRefused(f"the gateway refused every tag ({', '.join(reasons[:3])})")
        return feed

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

    async def _browse_many(self, node_ids: list[str]) -> list[list[dict]]:
        """Browse several nodes in one Browse request (plus BrowseNext for long child lists)."""
        params = ua.BrowseParameters()
        params.RequestedMaxReferencesPerNode = BROWSE_MAX_REFS
        for nid in node_ids:
            d = ua.BrowseDescription()
            d.NodeId = ua.NodeId.from_string(nid)
            d.BrowseDirection = ua.BrowseDirection.Forward
            d.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
            d.IncludeSubtypes = True
            d.NodeClassMask = ua.NodeClass.Object | ua.NodeClass.Variable
            d.ResultMask = ua.BrowseResultMask.All
            params.NodesToBrowse.append(d)
        results = await self.client.uaclient.browse(params)
        out = []
        for res in results:
            refs = list(res.References or [])
            cont = res.ContinuationPoint
            while cont:
                nxt = await self.client.uaclient.browse_next(_browse_next_params(cont))
                refs.extend(nxt[0].References or [])
                cont = nxt[0].ContinuationPoint
            out.append([{"node_id": r.NodeId.to_string(), "name": r.DisplayName.Text or r.BrowseName.Name,
                         "node_class": r.NodeClass.name} for r in refs])
        return out

    async def export(self, node_id: Optional[str] = None, progress=None) -> dict:
        """Walk a subtree and list every variable with its path, data type and current value.

        Browses level by level, EXPORT_BATCH nodes per request, so a whole controller
        takes a few hundred round trips rather than one per node. ``progress(visited, tags)``
        is called after each batch. This is the raw material for dashboards and machine
        profiles.
        """
        start = node_id or self.client.nodes.objects.nodeid.to_string()
        rows: list[dict] = []
        visited = 0
        truncated = False
        failed = 0  # folders that could not be browsed
        level: list[tuple[str, str]] = [(start, "")]
        seen = {start}
        depth = 0
        while level and depth < MAX_EXPORT_DEPTH and not truncated:
            next_level: list[tuple[str, str]] = []
            for i in range(0, len(level), EXPORT_BATCH):
                batch = level[i:i + EXPORT_BATCH]
                try:
                    children = await self._browse_many([nid for nid, _ in batch])
                except Exception:
                    # Retry one node at a time, so one refused node doesn't drop a whole batch of subtrees,
                    # and count what still failed: a quietly partial export makes a dashboard with holes.
                    children = []
                    for nid, _ in batch:
                        try:
                            children.append((await self._browse_many([nid]))[0])
                        except Exception:
                            failed += 1
                            children.append([])
                visited += len(batch)
                for (nid, path), kids in zip(batch, children):
                    for c in kids:
                        child_path = f"{path}/{c['name']}" if path else c["name"]
                        if c["node_class"] == "Variable":
                            rows.append({"path": child_path, "node_id": c["node_id"]})
                        if c["node_id"] not in seen:
                            seen.add(c["node_id"])
                            next_level.append((c["node_id"], child_path))
                if progress:
                    progress(visited, len(rows))
                if visited + len(next_level) > MAX_EXPORT_NODES:
                    truncated = True
                    break
            level = next_level
            depth += 1
        rows.sort(key=lambda r: r["path"].lower())
        for i in range(0, len(rows), MAX_READ):
            chunk = rows[i:i + MAX_READ]
            for row, val in zip(chunk, await self.read([r["node_id"] for r in chunk])):
                row.update(variant_type=val["variant_type"], value=val["value"], status=val["status"])
            if progress:
                progress(visited, len(rows))
        return {"tags": rows, "truncated": truncated, "failed": failed}


def _browse_next_params(cont: bytes) -> ua.BrowseNextParameters:
    p = ua.BrowseNextParameters()
    p.ReleaseContinuationPoints = False
    p.ContinuationPoints = [cont]
    return p


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

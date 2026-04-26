"""GFS cluster coordination (spec §24.10).

Symmetric peer-to-peer — no primary/leader. Every node runs the same
code, shares ``client_instances`` + ``global_spaces`` registries via
``NODE_SYNC_*`` messages, and fan-outs post relays via ``NODE_RELAY``.
State sync is last-write-wins with one exception: a ``banned`` record
always wins over any subsequent non-ban upsert.

Cluster mode is gated behind ``config.cluster_enabled``; single-node
deployments skip the background heartbeat loop but the service stays
callable so the admin portal's cluster tab + ``/cluster/health`` work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

from ..crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519
from .domain import ClientInstance, ClusterNode, GfsFraudReport, GlobalSpace

if TYPE_CHECKING:
    from .repositories import (
        AbstractClusterRepo,
        AbstractGfsAdminRepo,
        AbstractGfsFederationRepo,
    )

log = logging.getLogger(__name__)


HEARTBEAT_INTERVAL_S: int = 30
HEARTBEAT_FAIL_THRESHOLD: int = 3
SYNC_RETRY_DELAY_S: int = 5
CLUSTER_RATE_LIMIT_PER_MIN: int = 60

#: Spec §24.10.7 / S-8 — per-node ceiling on concurrent sync signaling
#: sessions. ``pick_signaling_node`` filters out any node already at this
#: count, and the GFS replies ``SPACE_SYNC_DIRECT_FAILED`` when nothing
#: remains.
MAX_SIGNALING_SESSIONS: int = 200


# ─── NODE_* message types ───────────────────────────────────────────────

NODE_HELLO = "NODE_HELLO"
NODE_HEARTBEAT = "NODE_HEARTBEAT"
NODE_SYNC_CLIENT = "NODE_SYNC_CLIENT"
NODE_SYNC_SPACE = "NODE_SYNC_SPACE"
NODE_SYNC_REPORT = "NODE_SYNC_REPORT"  # Phase Z — fraud aggregation
NODE_RELAY = "NODE_RELAY"
NODE_POLICY_PUSH = "NODE_POLICY_PUSH"


class ClusterService:
    """Spec-shape :class:`ClusterService`.

    Compatible with the earlier single-node stub: the legacy
    ``announce(node_id, address)`` / ``list_nodes()`` / ``get_leader()``
    methods still work when cluster mode is disabled.
    """

    __slots__ = (
        "_repo",
        "_admin_repo",
        "_fed_repo",
        "_node_id",
        "_self_url",
        "_peers",
        "_signing_key",
        "_own_pk_hex",
        "_enabled",
        "_heartbeat_task",
        "_fail_counts",
        "_seen_relays",
        "_active_sync_count",
    )

    def __init__(
        self,
        repo: "AbstractClusterRepo",
        *,
        admin_repo: "AbstractGfsAdminRepo | None" = None,
        fed_repo: "AbstractGfsFederationRepo | None" = None,
        node_id: str = "",
        self_url: str = "",
        peers: tuple[str, ...] = (),
        signing_key: bytes = b"",
        own_public_key_hex: str = "",
        enabled: bool = False,
    ) -> None:
        self._repo = repo
        self._admin_repo = admin_repo
        self._fed_repo = fed_repo
        self._node_id = node_id
        self._self_url = self_url
        self._peers = tuple(peers)
        self._signing_key = signing_key
        self._own_pk_hex = own_public_key_hex
        self._enabled = enabled
        self._heartbeat_task: asyncio.Task | None = None
        self._fail_counts: dict[str, int] = {}
        #: 10-minute TTL dedup for relayed message ids.
        self._seen_relays: dict[str, float] = {}
        #: Spec §24.10.7 — local view of each node's active sync-signaling
        #: load. The own count is authoritative; peer counts are refreshed
        #: from incoming ``NODE_HEARTBEAT`` payloads. Local-per-node, no
        #: cluster-wide consensus required.
        self._active_sync_count: dict[str, int] = {}

    # ─── Back-compat single-node stub API ─────────────────────────────

    async def announce(self, node_id: str, address: str) -> None:
        await self._repo.upsert_node(
            ClusterNode(
                node_id=node_id,
                url=address,
                status="online",
            )
        )

    async def list_nodes(self) -> list[ClusterNode]:
        return await self._repo.list_nodes()

    async def get_leader(self) -> str | None:
        return await self._repo.get_leader_id()

    # ─── Cluster lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        """Announce to seed peers + start the heartbeat loop.

        No-op if cluster mode is disabled.
        """
        if not self._enabled or not self._node_id:
            return
        await self._announce_to_peers()
        loop = asyncio.get_running_loop()
        self._heartbeat_task = loop.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError, Exception:
                pass
            self._heartbeat_task = None

    async def health(self) -> dict:
        """Return this node's cluster status (public ``GET /cluster/health``)."""
        rows = await self._repo.list_nodes()
        return {
            "node_id": self._node_id,
            "status": "online" if self._enabled else "single-node",
            "peers": [
                {
                    "node_id": r.node_id,
                    "url": r.url,
                    "status": r.status,
                    "last_seen": r.last_seen,
                }
                for r in rows
            ],
        }

    # ─── Sync-signaling round-robin (spec §24.10.7) ───────────────────

    async def pick_signaling_node(self) -> str | None:
        """Return the URL of the least-loaded cluster node, or ``None``.

        Implements the weighted least-connections selector from spec
        §24.10.7. Candidates are non-offline peers plus self; each
        candidate is filtered out when its ``_active_sync_count`` has
        reached :data:`MAX_SIGNALING_SESSIONS` (S-8). Sorting by
        ``(count, node_id)`` gives a deterministic tie-break.

        Returns ``None`` in three cases the caller must distinguish:

        * Single-node mode (``cluster_enabled = false``) — no peer to
          load-balance with; the SH provider should omit ``signaling_node``
          from ``SPACE_SYNC_OFFER`` (spec §24.10.7 "Non-cluster GFS").
        * No peer is currently online and ``self`` is also at cap — the
          GFS replies ``SPACE_SYNC_DIRECT_FAILED {reason: "node_capacity"}``.
        * Misconfiguration (own ``node_id``/``url`` blank) — fail safe.
        """
        if not self._enabled:
            return None
        if not self._node_id or not self._self_url:
            return None
        rows = await self._repo.list_nodes()
        candidates: list[tuple[int, str, str]] = []
        for r in rows:
            if r.status == "offline":
                continue
            if r.node_id == self._node_id:
                # Own row — prefer the in-memory authoritative count.
                continue
            count = self._active_sync_count.get(
                r.node_id,
                int(r.active_sync_sessions or 0),
            )
            if count >= MAX_SIGNALING_SESSIONS:
                continue
            candidates.append((count, r.node_id, r.url))
        own_count = self._active_sync_count.get(self._node_id, 0)
        if own_count < MAX_SIGNALING_SESSIONS:
            candidates.append((own_count, self._node_id, self._self_url))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    async def note_signaling_started(self, node_id: str) -> None:
        """Increment the active sync-signaling count for *node_id*.

        Called by the GFS REST handler the moment ``pick_signaling_node``
        commits to a node, so the next picker sees the updated load.
        For self the count is also persisted to ``cluster_nodes`` so the
        column in ``GET /cluster/health`` and admin UIs stays current.
        """
        if not node_id:
            return
        new_count = self._active_sync_count.get(node_id, 0) + 1
        self._active_sync_count[node_id] = new_count
        if node_id == self._node_id:
            await self._persist_own_count(new_count)

    async def note_signaling_ended(self, node_id: str) -> None:
        """Decrement the active sync-signaling count for *node_id*.

        Floor at zero — duplicate releases (e.g. both
        ``SPACE_SYNC_DIRECT_READY`` and ``SPACE_SYNC_DIRECT_FAILED``) are
        idempotent rather than producing negative counts. Persists to
        ``cluster_nodes`` for self only.
        """
        if not node_id:
            return
        new_count = max(0, self._active_sync_count.get(node_id, 0) - 1)
        self._active_sync_count[node_id] = new_count
        if node_id == self._node_id:
            await self._persist_own_count(new_count)

    async def _persist_own_count(self, count: int) -> None:
        if not self._enabled or not self._node_id:
            return
        try:
            await self._repo.update_active_sync_sessions(self._node_id, count)
        except Exception as exc:
            log.debug(
                "cluster: failed to persist active_sync_sessions for self: %s",
                exc,
            )

    # ─── Outbound NODE_* broadcasts ──────────────────────────────────

    async def sync_client(
        self,
        client: ClientInstance,
        *,
        action: str = "upsert",
    ) -> None:
        if not self._enabled:
            return
        await self._broadcast(
            NODE_SYNC_CLIENT,
            {
                "action": action,
                "client_instance": _client_to_wire(client),
            },
        )

    async def sync_space(
        self,
        space: GlobalSpace,
        *,
        action: str = "upsert",
    ) -> None:
        if not self._enabled:
            return
        await self._broadcast(
            NODE_SYNC_SPACE,
            {
                "action": action,
                "global_space": _space_to_wire(space),
            },
        )

    async def sync_report(self, report: GfsFraudReport) -> None:
        """Phase Z — propagate a fraud report to every peer."""
        if not self._enabled:
            return
        await self._broadcast(
            NODE_SYNC_REPORT,
            {
                "report": _report_to_wire(report),
            },
        )

    async def sync_policy(self, policy: dict) -> None:
        if not self._enabled:
            return
        await self._broadcast(NODE_POLICY_PUSH, policy)

    async def relay_to_peers(
        self,
        space_id: str,
        envelope: dict,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Forward a post-relay to peer nodes (fire-and-forget)."""
        if not self._enabled:
            return
        asyncio.create_task(
            self._broadcast(
                NODE_RELAY,
                {"space_id": space_id, "envelope": envelope},
                ignore_errors=True,
                session=session,
            )
        )

    # ─── Admin-portal entry points ────────────────────────────────────

    async def add_peer(self, peer_url: str) -> ClusterNode:
        """Admin added a peer URL via the portal. Upsert + send NODE_HELLO."""
        node = ClusterNode(
            node_id=peer_url,
            url=peer_url,
            status="unknown",
        )
        await self._repo.upsert_node(node)
        try:
            await self._post_to_peer(
                peer_url,
                NODE_HELLO,
                {
                    "node_id": self._node_id,
                    "url": self._self_url,
                    "public_key": self._own_pk_hex,
                },
                session=None,
            )
        except Exception:
            log.debug("cluster: initial NODE_HELLO to %s failed", peer_url)
        return node

    async def remove_peer(self, node_id: str) -> None:
        await self._repo.remove_node(node_id)

    async def ping_peer(self, peer_url: str) -> bool:
        return await self._ping_peer(peer_url)

    # ─── Inbound NODE_* handlers ─────────────────────────────────────

    async def handle_hello(
        self,
        from_node_id: str,
        url: str,
        public_key_hex: str,
    ) -> None:
        await self._repo.upsert_node(
            ClusterNode(
                node_id=from_node_id,
                url=url,
                public_key=public_key_hex,
                status="online",
                last_seen=_now_iso(),
            )
        )

    async def handle_heartbeat(
        self,
        from_node_id: str,
        payload: dict | None = None,
    ) -> None:
        """Refresh ``last_seen`` for *from_node_id* and capture its load.

        ``payload['active_sync_sessions']`` is the peer's
        authoritative sync-signaling count (spec §24.10.7). It is mirrored
        into our in-memory ``_active_sync_count`` so the next
        ``pick_signaling_node`` reflects fresh load, and persisted to the
        ``cluster_nodes`` row so admin UIs stay accurate.
        """
        peer_count: int | None = None
        if isinstance(payload, dict) and "active_sync_sessions" in payload:
            try:
                peer_count = max(0, int(payload["active_sync_sessions"]))
            except TypeError, ValueError:
                peer_count = None
        rows = await self._repo.list_nodes()
        for r in rows:
            if r.node_id == from_node_id:
                await self._repo.upsert_node(
                    ClusterNode(
                        node_id=r.node_id,
                        url=r.url,
                        public_key=r.public_key,
                        status="online",
                        last_seen=_now_iso(),
                        added_at=r.added_at,
                        active_sync_sessions=r.active_sync_sessions,
                    )
                )
                if peer_count is not None:
                    self._active_sync_count[from_node_id] = peer_count
                    await self._repo.update_active_sync_sessions(
                        from_node_id,
                        peer_count,
                    )
                return

    async def apply_sync_client(
        self,
        action: str,
        client_instance: dict,
    ) -> None:
        """Inbound NODE_SYNC_CLIENT — LWW with ban-wins rule."""
        if self._fed_repo is None:
            return
        instance = _wire_to_client(client_instance)
        existing = await self._fed_repo.get_instance(instance.instance_id)
        if existing is not None and existing.status == "banned" and action != "ban":
            return
        await self._fed_repo.upsert_instance(instance)

    async def apply_sync_space(
        self,
        action: str,
        global_space: dict,
    ) -> None:
        if self._fed_repo is None:
            return
        space = _wire_to_space(global_space)
        existing = await self._fed_repo.get_space(space.space_id)
        if existing is not None and existing.status == "banned" and action != "ban":
            return
        await self._fed_repo.upsert_space(space)

    async def apply_sync_report(self, report_dict: dict) -> None:
        """Inbound NODE_SYNC_REPORT — idempotent save via UNIQUE index."""
        if self._admin_repo is None:
            return
        try:
            report = _wire_to_report(report_dict)
        except KeyError, ValueError:
            return
        await self._admin_repo.save_fraud_report(report)

    async def apply_policy_push(self, policy: dict) -> None:
        if self._admin_repo is None:
            return
        for key in ("auto_accept_clients", "auto_accept_spaces", "fraud_threshold"):
            if key in policy:
                await self._admin_repo.set_config(key, str(policy[key]))

    async def apply_relay(self, space_id: str, envelope: dict) -> None:
        """Inbound NODE_RELAY — dedup; local fan-out lives in the
        federation service.
        """
        msg_id = str(envelope.get("msg_id") or envelope.get("message_id") or "")
        if msg_id and msg_id in self._seen_relays:
            return
        if msg_id:
            self._seen_relays[msg_id] = time.monotonic()
            self._gc_seen()

    # ─── Internals ────────────────────────────────────────────────────

    async def _announce_to_peers(self) -> None:
        msg = {
            "node_id": self._node_id,
            "url": self._self_url,
            "public_key": self._own_pk_hex,
        }
        for peer_url in self._peers:
            try:
                await self._post_to_peer(peer_url, NODE_HELLO, msg, session=None)
            except Exception as exc:
                log.debug("cluster: NODE_HELLO to %s failed: %s", peer_url, exc)

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                rows = await self._repo.list_nodes()
                for r in rows:
                    if r.status == "offline":
                        continue
                    ok = await self._ping_peer(r.url)
                    fails = self._fail_counts.get(r.url, 0)
                    if ok:
                        self._fail_counts[r.url] = 0
                        await self._repo.upsert_node(
                            ClusterNode(
                                node_id=r.node_id,
                                url=r.url,
                                public_key=r.public_key,
                                status="online",
                                last_seen=_now_iso(),
                                added_at=r.added_at,
                                active_sync_sessions=r.active_sync_sessions,
                            )
                        )
                        # Spec §24.10.7 — propagate own sync-signaling load
                        # via NODE_HEARTBEAT so peers' selectors see fresh
                        # counts on the next ``pick_signaling_node``.
                        try:
                            await self._post_to_peer(
                                r.url,
                                NODE_HEARTBEAT,
                                {
                                    "active_sync_sessions": self._active_sync_count.get(
                                        self._node_id,
                                        0,
                                    ),
                                },
                                session=None,
                            )
                        except Exception as exc:
                            log.debug(
                                "cluster: NODE_HEARTBEAT to %s failed: %s",
                                r.url,
                                exc,
                            )
                    else:
                        self._fail_counts[r.url] = fails + 1
                        if fails + 1 >= HEARTBEAT_FAIL_THRESHOLD:
                            await self._repo.upsert_node(
                                ClusterNode(
                                    node_id=r.node_id,
                                    url=r.url,
                                    public_key=r.public_key,
                                    status="offline",
                                    last_seen=r.last_seen,
                                    added_at=r.added_at,
                                    active_sync_sessions=r.active_sync_sessions,
                                )
                            )
        except asyncio.CancelledError:
            return

    async def _broadcast(
        self,
        msg_type: str,
        payload: dict,
        *,
        ignore_errors: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        rows = await self._repo.list_nodes()
        for r in rows:
            if r.status == "offline":
                continue
            try:
                await self._post_to_peer(r.url, msg_type, payload, session=session)
                self._fail_counts[r.url] = 0
            except Exception as exc:
                if ignore_errors:
                    log.debug("cluster: %s to %s failed: %s", msg_type, r.url, exc)
                    continue
                await asyncio.sleep(SYNC_RETRY_DELAY_S)
                try:
                    await self._post_to_peer(
                        r.url,
                        msg_type,
                        payload,
                        session=session,
                    )
                except Exception as exc2:
                    log.warning(
                        "cluster: %s to %s dropped after retry: %s",
                        msg_type,
                        r.url,
                        exc2,
                    )

    async def _post_to_peer(
        self,
        peer_url: str,
        msg_type: str,
        payload: dict,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        body = {
            "type": msg_type,
            "from": self._node_id,
            "ts": int(time.time()),
            "payload": payload,
        }
        canonical = json.dumps(
            body,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        sig = (
            b64url_encode(sign_ed25519(self._signing_key, canonical))
            if self._signing_key
            else ""
        )
        own_session = session is None
        active = session if session is not None else aiohttp.ClientSession()
        try:
            async with active.post(
                f"{peer_url.rstrip('/')}/cluster/sync",
                data=canonical,
                headers={
                    "Content-Type": "application/json",
                    "X-Node-Signature": sig,
                    "X-Node-Id": self._node_id,
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"peer {peer_url} returned {resp.status}")
        finally:
            if own_session:
                await active.close()

    async def _ping_peer(self, peer_url: str) -> bool:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"{peer_url.rstrip('/')}/cluster/health",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return 200 <= resp.status < 300
        except Exception:
            return False

    def _gc_seen(self) -> None:
        cutoff = time.monotonic() - 600.0
        stale = [k for k, v in self._seen_relays.items() if v < cutoff]
        for k in stale:
            self._seen_relays.pop(k, None)


# ─── Wire shape helpers ──────────────────────────────────────────────────


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _client_to_wire(c: ClientInstance) -> dict:
    return {
        "instance_id": c.instance_id,
        "display_name": c.display_name,
        "public_key": c.public_key,
        "inbox_url": c.inbox_url,
        "status": c.status,
        "auto_accept": c.auto_accept,
        "connected_at": c.connected_at,
    }


def _wire_to_client(d: dict) -> ClientInstance:
    return ClientInstance(
        instance_id=str(d["instance_id"]),
        display_name=str(d.get("display_name") or ""),
        public_key=str(d.get("public_key") or ""),
        inbox_url=str(d.get("inbox_url") or ""),
        status=str(d.get("status") or "pending"),
        auto_accept=bool(d.get("auto_accept") or False),
        connected_at=str(d.get("connected_at") or ""),
    )


def _space_to_wire(s: GlobalSpace) -> dict:
    return {
        "space_id": s.space_id,
        "owning_instance": s.owning_instance,
        "name": s.name,
        "description": s.description,
        "about_markdown": s.about_markdown,
        "cover_url": s.cover_url,
        "min_age": s.min_age,
        "target_audience": s.target_audience,
        "accent_color": s.accent_color,
        "status": s.status,
        "subscriber_count": s.subscriber_count,
        "posts_per_week": s.posts_per_week,
        "published_at": s.published_at,
    }


def _wire_to_space(d: dict) -> GlobalSpace:
    return GlobalSpace(
        space_id=str(d["space_id"]),
        owning_instance=str(d.get("owning_instance") or ""),
        name=str(d.get("name") or ""),
        description=d.get("description"),
        about_markdown=d.get("about_markdown"),
        cover_url=d.get("cover_url"),
        min_age=int(d.get("min_age") or 0),
        target_audience=str(d.get("target_audience") or "all"),
        accent_color=str(d.get("accent_color") or "#6366f1"),
        status=str(d.get("status") or "pending"),
        subscriber_count=int(d.get("subscriber_count") or 0),
        posts_per_week=float(d.get("posts_per_week") or 0.0),
        published_at=str(d.get("published_at") or ""),
    )


def _report_to_wire(r: GfsFraudReport) -> dict:
    return {
        "id": r.id,
        "target_type": r.target_type,
        "target_id": r.target_id,
        "category": r.category,
        "notes": r.notes,
        "reporter_instance_id": r.reporter_instance_id,
        "reporter_user_id": r.reporter_user_id,
        "status": r.status,
        "created_at": r.created_at,
    }


def _wire_to_report(d: dict) -> GfsFraudReport:
    return GfsFraudReport(
        id=str(d["id"]),
        target_type=str(d["target_type"]),
        target_id=str(d["target_id"]),
        category=str(d["category"]),
        notes=d.get("notes"),
        reporter_instance_id=str(d["reporter_instance_id"]),
        reporter_user_id=d.get("reporter_user_id"),
        status=str(d.get("status") or "pending"),
        created_at=int(d.get("created_at") or time.time()),
    )


# ─── Signature verification for inbound NODE_* ───────────────────────────


def verify_node_signature(
    canonical_body: bytes,
    signature_b64url: str,
    public_key_hex: str,
) -> bool:
    if not signature_b64url or not public_key_hex:
        return False
    try:
        raw_key = bytes.fromhex(public_key_hex)
        raw_sig = b64url_decode(signature_b64url)
    except ValueError, TypeError:
        return False
    return verify_ed25519(raw_key, canonical_body, raw_sig)

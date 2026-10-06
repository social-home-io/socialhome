"""GFS cluster coordination (spec §24.10).

Symmetric peer-to-peer — no primary/leader. Every node runs the same
code, shares ``client_instances`` + ``global_spaces`` registries via
``NODE_SYNC_*`` messages, and fan-outs post relays via ``NODE_RELAY``.
State sync is last-write-wins with two exceptions: a ``banned`` record
always wins over any subsequent non-ban upsert, and a locally-``withdrawn``
space stays withdrawn against a peer's stale ``withdrawn=0`` row.

Cluster mode is gated behind ``config.cluster_enabled``; single-node
deployments skip the background heartbeat loop but the service stays
callable so the admin portal's cluster tab + ``/cluster/health`` work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..authority_cert import MAX_AUTHORITY_KEY_EPOCH
from ..crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519
from ..domain.space import normalize_category, normalize_join_mode
from ..capabilities_sig import sign_capabilities
from .domain import ClientInstance, ClusterNode, GfsFraudReport, GlobalSpace
from .federation import SeenPayloadCache, certified_authority_repin
from .public import SlidingWindowCounter

if TYPE_CHECKING:
    from .repositories import (
        AbstractClusterRepo,
        AbstractGfsAdminRepo,
        AbstractGfsFederationRepo,
    )
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)


HEARTBEAT_INTERVAL_S: int = 30
HEARTBEAT_FAIL_THRESHOLD: int = 3
SYNC_RETRY_DELAY_S: int = 5
#: Spec §24.10.4 — ``/cluster/sync`` messages per minute per VERIFIED peer
#: node (signature checked against the key pinned for that node). Never keyed
#: on a claimed id: an unverified request must not spend a real node's budget.
CLUSTER_RATE_LIMIT_PER_MIN: int = 60

#: ``/cluster/sync`` requests per minute per source address that did NOT prove
#: a member: malformed bodies, unknown suites, stale timestamps, unknown or
#: unapproved senders, key mismatches, bad signatures and replays. Once spent,
#: the address is shed before any parse, DB read or signature verification,
#: which bounds the verify CPU a forged flood can burn. Genuine peers never
#: touch it, so 30/min is far above real use.
CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN: int = 30

#: ``/cluster/sync`` frames carry the sender's wall-clock ``ts`` (unix
#: seconds, int) inside the signed body. A frame more than this far from the
#: receiver's wall clock is refused (401 ``stale_timestamp``), which bounds
#: how long a captured frame can be replayed. Cluster nodes must keep their
#: clocks within this window of each other (NTP) or they partition.
CLUSTER_TS_SKEW_S: int = 300

#: Signature suite of a ``/cluster/sync`` frame, carried as ``sig_suite`` in
#: the signed body. Today the GFS identity key is Ed25519; a PQ-hybrid suite
#: (``ed25519+mldsa65``) is added by growing the frozenset. A receiver rejects
#: any suite it does not list — no default fallback — but a frame with NO
#: ``sig_suite`` is from a sender older than the field and can only be Ed25519.
CLUSTER_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_CLUSTER_SIG_SUITES: frozenset[str] = frozenset({CLUSTER_SIG_SUITE_ED25519})

#: Random bytes in each frame's ``nonce`` — two frames sent in the same
#: second differ, so the replay cache never mistakes them for one.
CLUSTER_NONCE_BYTES: int = 16


class UnsupportedClusterSigSuite(ValueError):
    """A ``/cluster/sync`` frame named a signature suite we don't support."""


def parse_cluster_sig_suite(raw: object) -> str:
    """Return the frame's suite; ``None`` (field absent) → Ed25519.

    Raises :class:`UnsupportedClusterSigSuite` for anything not in
    :data:`SUPPORTED_CLUSTER_SIG_SUITES`, including a non-string.
    """
    if raw is None:
        return CLUSTER_SIG_SUITE_ED25519
    if not isinstance(raw, str) or raw not in SUPPORTED_CLUSTER_SIG_SUITES:
        raise UnsupportedClusterSigSuite(f"unsupported cluster sig suite: {raw!r}")
    return raw


#: Slack below this process's start time for the boot floor: a frame whose
#: ``ts`` predates our start by more than this is refused, because the
#: in-memory replay cache was empty then and cannot vouch for it.
CLUSTER_BOOT_FLOOR_SLACK_S: int = 5

#: How long an accepted frame's digest is remembered. A frame is fresh for
#: ``±CLUSTER_TS_SKEW_S`` around its ``ts``, so the cache must outlive the
#: whole 600 s window or a captured frame would replay after expiring here.
CLUSTER_REPLAY_TTL_S: float = float(2 * CLUSTER_TS_SKEW_S)

#: Roster size the replay cache is sized for. Only ACCEPTED frames are
#: recorded, and each verified node is capped at
#: :data:`CLUSTER_RATE_LIMIT_PER_MIN`, so this many saturating nodes fit
#: within one TTL without evicting a live digest. A bigger roster that
#: saturates every budget at once would evict the oldest digests early.
CLUSTER_REPLAY_SIZED_NODES: int = 32

#: Replay-cache capacity: every frame the sized roster can have accepted
#: within one TTL (60/min × 10 min × 32 = 19 200 digests, ~3 MB).
CLUSTER_REPLAY_MAX_ENTRIES: int = (
    CLUSTER_RATE_LIMIT_PER_MIN
    * int(CLUSTER_REPLAY_TTL_S // 60)
    * CLUSTER_REPLAY_SIZED_NODES
)

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
#: Spec §24.10.3 / §4.4.6 — partition-healing pair. ``CATCHUP`` is sent
#: by a node that just came back online; the receiver replies with one
#: ``GAP`` per space where its own ``last_relay_ts`` is newer than the
#: sender's (fire-and-discard posts can't be replayed; subscribers see
#: a banner instead).
NODE_PARTITION_CATCHUP = "NODE_PARTITION_CATCHUP"
NODE_PARTITION_GAP = "NODE_PARTITION_GAP"


#: Longest ``node_id`` the admin API accepts for a peer.
CLUSTER_NODE_ID_MAX_LEN: int = 128

_HEX_ED25519_KEY = re.compile(r"[0-9a-f]{64}")


class InvalidClusterPeer(ValueError):
    """Admin add-peer input is malformed; ``code`` is the API error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ClusterPeerKeyMismatch(Exception):
    """Admin add-peer named a node already pinned to a different key."""


def _validated_peer(
    node_id: object, url: object, public_key: object, *, own_node_id: str
) -> tuple[str, str, str]:
    """Normalise + validate an admin add-peer request, or raise
    :class:`InvalidClusterPeer`. Returns ``(node_id, url, public_key)``."""
    if not isinstance(node_id, str):
        raise InvalidClusterPeer("invalid_node_id")
    node_id = node_id.strip()
    if not node_id or len(node_id) > CLUSTER_NODE_ID_MAX_LEN:
        raise InvalidClusterPeer("invalid_node_id")
    if node_id == own_node_id:
        raise InvalidClusterPeer("node_id_is_self")
    if not isinstance(url, str):
        raise InvalidClusterPeer("invalid_url")
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidClusterPeer("invalid_url")
    if not isinstance(public_key, str):
        raise InvalidClusterPeer("invalid_public_key")
    key = public_key.strip().lower()
    if not _HEX_ED25519_KEY.fullmatch(key):
        raise InvalidClusterPeer("invalid_public_key")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key))
    except ValueError as exc:
        raise InvalidClusterPeer("invalid_public_key") from exc
    return node_id, url, key


# ─── Membership rule ─────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class FrameVerdict:
    """Outcome of :func:`authorize_frame`.

    Exactly one field is set: ``verify_key`` (hex Ed25519 key the frame's
    signature must verify under) when the sender is a member, else
    ``error`` — ``unknown_node``, ``unapproved_node`` or ``key_mismatch``.
    """

    verify_key: str = ""
    error: str = ""


def authorize_frame(
    *,
    msg_type: str,
    from_node: str,
    carried_key: str,
    pinned: ClusterNode | None,
    own_key: str,
) -> FrameVerdict:
    """Decide which key a ``/cluster/sync`` frame must verify under (§24.10).

    Pure: no I/O, no logging. A node is a cluster member if and only if its
    frames verify under a key this GFS already holds:

    * our OWN identity key — the shared seed; an operator who gave a node
      the seed approved it; or
    * the key an operator pinned on the node's ``cluster_nodes`` row
      (``POST /admin/api/cluster/peers``), or a grandfathered pin.

    ``NODE_HELLO`` names the key it is signed under (``carried_key``):

    * the node's non-empty pin → member; any other key → ``key_mismatch``
      (a pin never moves in-band, not even to our own key — rotation is
      delete then re-add);
    * no row, or an empty pin → member only under our own key, else
      ``unapproved_node`` (the caller writes nothing).

    Every other frame needs a row (``unknown_node`` otherwise) and verifies
    under its pin, or under our own key when the row carries none (a
    shared-seed sibling).
    """
    own = own_key.lower()
    if msg_type == NODE_HELLO:
        carried = carried_key.lower()
        pin = (pinned.public_key if pinned is not None else "").lower()
        if pin:
            if carried == pin:
                return FrameVerdict(verify_key=pin)
            return FrameVerdict(error="key_mismatch")
        if own and carried == own:
            return FrameVerdict(verify_key=own)
        return FrameVerdict(error="unapproved_node")
    if pinned is None:
        return FrameVerdict(error="unknown_node")
    key = pinned.public_key.lower() or own
    if not key:
        return FrameVerdict(error="unknown_node")
    return FrameVerdict(verify_key=key)


class ClusterService:
    """Spec-shape :class:`ClusterService`.

    All nodes are equal — no leader election or consensus protocol
    (spec §28431). ``announce`` / ``list_nodes`` work whether cluster
    mode is enabled or not.
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
        "_announce_task",
        "_stop",
        "_fail_counts",
        "_seen_relays",
        "_active_sync_count",
        "_local_last_relay_ts",
        "_partition_gaps",
        "_ws_registry",
        "_connected_clients",
        "_clock",
        "_sync_node_limiter",
        "_sync_unverified_limiter",
        "_wall_clock",
        "_process_start",
        "_seen_frames",
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
        ws_registry: "GfsWebSocketRegistry | None" = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
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
        self._announce_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._fail_counts: dict[str, int] = {}
        #: 10-minute TTL dedup for relayed message ids.
        self._seen_relays: dict[str, float] = {}
        #: Spec §24.10.7 — local view of each node's active sync-signaling
        #: load. The own count is authoritative; peer counts are refreshed
        #: from incoming ``NODE_HEARTBEAT`` payloads. Local-per-node, no
        #: cluster-wide consensus required.
        self._active_sync_count: dict[str, int] = {}
        #: Spec §4.4.6 / §24.10.3 — last unix-ts at which we relayed
        #: anything for ``space_id``. Updated by ``record_relay_ts`` (the
        #: federation service calls this on successful relay). Used to
        #: emit ``NODE_PARTITION_GAP`` when a peer's ``CATCHUP`` payload
        #: shows it missed posts during a partition. In-memory only —
        #: a restart loses the timeline, which is acceptable since
        #: gap-reporting is best-effort.
        self._local_last_relay_ts: dict[str, float] = {}
        #: Inbound ``NODE_PARTITION_GAP`` accumulator. Keyed by
        #: ``space_id``; each value is the most recent gap range we saw
        #: from any peer. Read by
        #: :meth:`pending_partition_gaps` so SH-side fan-out can drain
        #: them after a partition heals.
        self._partition_gaps: dict[str, dict] = {}
        self._ws_registry = ws_registry
        #: Spec §24.10 — local view of each peer's live connected-client
        #: count (the GFS-side WebSocket sessions, one per paired SH
        #: household). Own count is read live from the ws-registry; peer
        #: counts are refreshed from incoming ``NODE_HEARTBEAT`` payloads.
        #: In-memory only — ephemeral, never persisted.
        self._connected_clients: dict[str, int] = {}
        #: Monotonic clock for the ``/cluster/sync`` windows — injectable so
        #: a window test never depends on wall time.
        self._clock = clock
        #: Unix wall clock for the frame ``ts`` window (signed by the sender,
        #: checked by the receiver) — injectable so tests never race it.
        self._wall_clock = wall_clock
        #: Boot floor (see :data:`CLUSTER_BOOT_FLOOR_SLACK_S`).
        self._process_start = wall_clock()
        #: Digests of accepted ``/cluster/sync`` frames (raw signed bytes).
        #: In-memory, per process — the boot floor covers a restart.
        self._seen_frames = SeenPayloadCache(
            ttl_s=CLUSTER_REPLAY_TTL_S,
            cap=CLUSTER_REPLAY_MAX_ENTRIES,
        )
        #: Per VERIFIED node id (spec §24.10.4). Capped LRU, like every GFS
        #: limiter, though only proven peers ever get a bucket here.
        self._sync_node_limiter = SlidingWindowCounter(CLUSTER_RATE_LIMIT_PER_MIN)
        #: Per source address, spent only by requests that proved no known
        #: peer (see :data:`CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN`).
        self._sync_unverified_limiter = SlidingWindowCounter(
            CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN
        )

    # ─── /cluster/sync budgets ───────────────────────────────────────

    def sync_source_exhausted(self, client_ip: str) -> bool:
        """Whether *client_ip* has spent its unverified budget.

        Read-only — checked first, before the body is parsed or any
        signature verified, so a shed request costs nothing.
        """
        return self._sync_unverified_limiter.exhausted(client_ip, now=self._clock())

    def charge_unverified_sync(self, client_ip: str) -> bool:
        """Spend one unit of *client_ip*'s unverified budget.

        Called for every request that did not prove a member. Returns
        whether the request is still within budget (a request already being
        rejected ignores it).
        """
        return self._sync_unverified_limiter.allow(client_ip, now=self._clock())

    def charge_verified_sync(self, node_id: str) -> bool:
        """Spend one unit of a VERIFIED peer's budget; ``False`` → 429.

        *node_id* must be the id whose pinned key just verified the
        request's signature — never a claimed one.
        """
        return self._sync_node_limiter.allow(node_id, now=self._clock())

    def frame_ts_error(self, ts: object) -> str:
        """Check a ``/cluster/sync`` frame's signed ``ts``; ``""`` if fine.

        ``invalid_timestamp`` — not an int (a bool, float or string is
        malformed, never coerced). ``stale_timestamp`` — more than
        :data:`CLUSTER_TS_SKEW_S` from our wall clock, either way.
        """
        if isinstance(ts, bool) or not isinstance(ts, int):
            return "invalid_timestamp"
        if abs(ts - self._wall_clock()) > CLUSTER_TS_SKEW_S:
            return "stale_timestamp"
        if ts < self._process_start - CLUSTER_BOOT_FLOOR_SLACK_S:
            return "stale_timestamp"
        return ""

    def frame_seen(self, raw: bytes) -> bool:
        """Whether these exact signed frame bytes were already accepted."""
        return self._seen_frames.seen(_frame_digest(raw), now=self._clock())

    def record_frame(self, raw: bytes) -> None:
        """Remember an accepted frame so a byte-identical resend is refused.

        Called only once the frame passed every check, right before
        dispatch — a rejected frame never poisons the cache.
        """
        self._seen_frames.record(_frame_digest(raw), now=self._clock())

    def _own_connected_clients(self) -> int:
        return (
            self._ws_registry.connection_count() if self._ws_registry is not None else 0
        )

    @property
    def node_id(self) -> str:
        """This node's cluster id (unique per node)."""
        return self._node_id

    @property
    def own_public_key_hex(self) -> str:
        """Hex-encoded Ed25519 public key for this GFS instance.

        Exposed so the public ``GET /gfs/info`` endpoint can publish the
        key HFS clients pin during pairing. The matching private key is
        derived from the GFS ``instance_id`` and never leaves the
        process.
        """
        return self._own_pk_hex

    def sign_capabilities_block(
        self,
        gfs_instance_id: str,
        capabilities: dict,
    ) -> tuple[str, str]:
        """Sign this GFS's capability block with its own identity key.

        Returns ``(signature_b64url, suite)``, or ``("", "")`` when no signing
        key is wired — a build with no identity can't authenticate anything,
        so ``GET /gfs/info`` omits the block rather than shipping an
        unverifiable one, and paired households keep the (safe, identified)
        legacy relay body.

        The seed stays inside this service — it already owns the GFS identity
        keypair whose public half ``own_public_key_hex`` publishes and every
        household pins at pair time. No new key is minted for capabilities.
        """
        if not self._signing_key:
            return "", ""
        return sign_capabilities(self._signing_key, gfs_instance_id, capabilities)

    # ─── Node registry API ────────────────────────────────────────────

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

    # ─── Cluster lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        """Announce to seed peers + start the heartbeat loop.

        No-op (beyond the pin audit) if cluster mode is disabled.
        """
        await self._warn_foreign_pins()
        if not self._enabled or not self._node_id:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._stop.clear()
        await self._announce_to_peers()
        loop = asyncio.get_running_loop()
        self._heartbeat_task = loop.create_task(self._heartbeat_loop())
        # Re-announce loop: the one-shot announce above is lost if a peer is
        # not yet listening (a cold-start race — a sibling alloc that boots a
        # second later, or announces before its own HTTP server binds). Nothing
        # else recovers it: the heartbeat loop only pings peers already in the
        # DB, and a HELLO is the only thing that puts one there. So keep
        # HELLOing configured peers we do not yet know until they answer.
        self._announce_task = loop.create_task(self._reannounce_loop())

    async def _warn_foreign_pins(self) -> None:
        """WARN about peers trusted under a key that is not our own.

        Before membership needed operator approval, a first-contact HELLO
        pinned whatever key it carried (TOFU). Those rows are kept — they
        keep syncing, so an upgrade never partitions a working cluster —
        but nobody approved them. An admin-pinned key looks the same on
        disk, so the list is for the operator to confirm, not a verdict.
        """
        own = self._own_pk_hex.lower()
        foreign = [
            r.node_id
            for r in await self._repo.list_nodes()
            if _key_source(r.public_key, own) == "pinned"
        ]
        if foreign:
            log.warning(
                "cluster: %d peer(s) are trusted under a key that is not this "
                "GFS's own: %s. A pin like this was either added by an admin "
                "or grandfathered from first-contact TOFU before cluster "
                "membership needed approval — remove any you do not recognise "
                "(DELETE /admin/api/cluster/peers/{node_id}).",
                len(foreign),
                ", ".join(sorted(foreign)),
            )

    async def stop(self) -> None:
        self._stop.set()
        for attr in ("_heartbeat_task", "_announce_task"):
            task = getattr(self, attr)
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.TimeoutError, asyncio.CancelledError:
                    task.cancel()
                except Exception:  # pragma: no cover
                    pass
                setattr(self, attr, None)

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

    async def admin_cluster(self) -> dict:
        """Return the enriched cluster view for the admin portal.

        Richer than the public :meth:`health` — includes THIS node plus
        every peer, with live connected-client counts and the in-memory
        sync-signaling load. Admin-only. ``connected_clients`` for self is
        read live from the ws-registry; peer counts come from the most
        recent ``NODE_HEARTBEAT`` (0 if none seen yet). All counts are
        ephemeral, never persisted.
        """
        rows = await self._repo.list_nodes()
        self_status = "online" if self._enabled else "single-node"
        nodes: list[dict] = []
        saw_self = False
        for r in rows:
            is_self = r.node_id == self._node_id
            if is_self:
                saw_self = True
            nodes.append(
                {
                    "node_id": r.node_id,
                    "url": r.url,
                    "status": self_status if is_self else r.status,
                    "last_seen": r.last_seen,
                    "connected_clients": (
                        self._own_connected_clients()
                        if is_self
                        else self._connected_clients.get(r.node_id, 0)
                    ),
                    "active_sync_sessions": (
                        self._active_sync_count.get(r.node_id, 0)
                        if is_self
                        else self._active_sync_count.get(
                            r.node_id,
                            int(r.active_sync_sessions or 0),
                        )
                    ),
                    "is_self": is_self,
                    "public_key": self._own_pk_hex if is_self else r.public_key,
                    "key_source": (
                        "own"
                        if is_self
                        else _key_source(r.public_key, self._own_pk_hex.lower())
                    ),
                }
            )
        if not saw_self:
            nodes.insert(
                0,
                {
                    "node_id": self._node_id,
                    "url": self._self_url,
                    "status": self_status,
                    "last_seen": None,
                    "connected_clients": self._own_connected_clients(),
                    "active_sync_sessions": self._active_sync_count.get(
                        self._node_id,
                        0,
                    ),
                    "is_self": True,
                    "public_key": self._own_pk_hex,
                    "key_source": "own",
                },
            )
        return {
            "node_id": self._node_id,
            # Our own identity key — what an operator pins for this node on
            # every other node (``POST /admin/api/cluster/peers``).
            "public_key": self._own_pk_hex,
            "status": self_status,
            "nodes": nodes,
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

    async def add_peer(
        self,
        node_id: object,
        url: object,
        public_key: object,
    ) -> ClusterNode:
        """Operator approval of a peer node: pin its key, then HELLO it.

        The pinned key is the node's cluster membership — its frames verify
        under it (:func:`authorize_frame`). A node already pinned to a
        different key raises :class:`ClusterPeerKeyMismatch` (rotation is
        :meth:`remove_peer` then re-add); bad input raises
        :class:`InvalidClusterPeer`. Re-adding the same key only refreshes
        the URL.
        """
        node_id, url, key = _validated_peer(
            node_id, url, public_key, own_node_id=self._node_id
        )
        existing = next(
            (n for n in await self._repo.list_nodes() if n.node_id == node_id),
            None,
        )
        if existing is not None and existing.public_key:
            if existing.public_key.lower() != key:
                raise ClusterPeerKeyMismatch(node_id)
            node = replace(existing, url=url)
        else:
            node = ClusterNode(node_id=node_id, url=url, public_key=key)
        await self._repo.upsert_node(node)
        try:
            await self._post_to_peer(
                url,
                NODE_HELLO,
                {
                    "node_id": self._node_id,
                    "url": self._self_url,
                    "public_key": self._own_pk_hex,
                },
                session=None,
            )
        except Exception as exc:
            # Expected until the operator approves us on the other side too;
            # its own add-peer HELLO then reaches us and we answer it.
            log.debug("cluster: initial NODE_HELLO to %s failed: %s", url, exc)
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
        """Record a member's HELLO (online, ``last_seen`` now).

        Only called once :func:`authorize_frame` accepted the HELLO and its
        signature verified, so *public_key_hex* is a key this node already
        held; the upsert never moves an existing pin anyway.
        """
        # Discovery must be bidirectional on first contact. ``_announce_to_peers``
        # only fires once, at startup, against the CONFIGURED peer URLs — so a
        # node whose peer was still down at that instant loses that HELLO and is
        # never re-announced to (the heartbeat loop only pings peers already in
        # the DB, and a HELLO is the only thing that puts one there). The result
        # was a cold-start deadlock: whichever node came up first stayed unknown
        # to the other, its heartbeats rejected 403 ``unknown_node`` forever, and
        # cross-alloc WS-push routing (the whole reason cluster mode is on for a
        # shared-DB deployment) silently didn't work for it. So when we learn a
        # peer we did NOT already know, we HELLO back — one round-trip, and both
        # sides converge the moment EITHER announces, whatever the boot order.
        # The Nomad peer template renders ``nomadService "gfs"`` — which
        # includes THIS alloc — so a node HELLOs itself. Never register self
        # as a peer: it would inflate ``cluster_nodes``, make a node heartbeat
        # itself, and show up peering with itself on ``/cluster/health``. The
        # ``node_id`` is unique per alloc, so it is the reliable self-check
        # (the URL may not match ``base_url`` exactly).
        if from_node_id == self._node_id:
            return
        # "Known" means we have heard from it before. An admin-added row has
        # never been seen (``last_seen`` is None) — answer its first HELLO so
        # the two sides converge whichever the operator added first.
        existing = await self._repo.list_nodes()
        already_known = any(
            n.node_id == from_node_id and n.last_seen is not None for n in existing
        )
        await self._repo.upsert_node(
            ClusterNode(
                node_id=from_node_id,
                url=url,
                public_key=public_key_hex,
                status="online",
                last_seen=_now_iso(),
            )
        )
        if not already_known and url and self._enabled and self._node_id:
            # Reply only on FIRST contact so this can't ping-pong: the peer
            # already knows us by the time it processes this, so its own
            # handle_hello takes the ``already_known`` branch and stops.
            try:
                await self._post_to_peer(
                    url,
                    NODE_HELLO,
                    {
                        "node_id": self._node_id,
                        "url": self._self_url,
                        "public_key": self._own_pk_hex,
                    },
                    session=None,
                )
            except Exception as exc:
                log.debug("cluster: reply NODE_HELLO to %s failed: %s", url, exc)

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
        # Connected-client count is fail-soft: absent on older peers, in
        # which case we leave any prior value untouched (never clobber to 0).
        peer_clients: int | None = None
        if isinstance(payload, dict) and "connected_clients" in payload:
            try:
                peer_clients = max(0, int(payload["connected_clients"]))
            except TypeError, ValueError:
                peer_clients = None
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
                if peer_clients is not None:
                    self._connected_clients[from_node_id] = peer_clients
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
        # Withdrawn-wins, mirroring ban-wins above: a peer gossiping a stale
        # ``withdrawn=0`` row must not silently re-list a space its owner
        # delisted here. Only the owner's own signed re-publish clears it.
        #
        # TODO: ``sync_space`` has NO production sender today, so this guard is
        # currently unreachable. Whoever wires cluster space gossip MUST mirror
        # the ban-wins escape hatch above: have the publish path send
        # ``action="publish"`` and guard this branch with
        # ``and action != "publish"``. Without that, an owner's re-publish
        # landing on node B is forced back to withdrawn by node A's gossip and
        # the space is stuck invisible cluster-wide with no way out.
        if existing is not None and existing.withdrawn and not space.withdrawn:
            space = replace(space, withdrawn=True)
        # v_44 — a peer node's gossip never moves a pin on its own say-so:
        # the space's OWNER must have certified the new key (checked here
        # against the owner's registered key, exactly like a publish). The
        # upsert keeps the stored pin + cert; a verified cert re-pins after.
        owner_id = existing.owning_instance if existing else space.owning_instance
        owner = await self._fed_repo.get_instance(owner_id)
        certified = owner is not None and certified_authority_repin(
            space.space_id,
            owning_instance=owner_id,
            owner_pk_hex=owner.public_key,
            offered_pk=space.identity_public_key,
            cert=space.authority_cert,
            stored_cert=existing.authority_cert if existing else None,
        )
        wire_cert = space.authority_cert
        space = replace(
            space, authority_cert=existing.authority_cert if existing else None
        )
        await self._fed_repo.upsert_space(space)
        if certified and wire_cert is not None:
            stored = await self._fed_repo.get_space(space.space_id)
            if stored is not None:
                await self._fed_repo.set_space_authority(
                    space.space_id,
                    expected_pk=stored.identity_public_key,
                    expected_cert=stored.authority_cert,
                    new_pk=space.identity_public_key,
                    cert=wire_cert,
                )
        # Max-merge the rotation seq, for the pin this node now holds only:
        # a re-pin bumped ours by one, but the publishing node may be further
        # along, and a stale gossip must never move it backwards.
        if space.authority_rotation_seq > 0:
            await self._fed_repo.raise_authority_rotation_seq(
                space.space_id,
                pk=space.identity_public_key,
                seq=space.authority_rotation_seq,
            )

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
        if space_id:
            self._local_last_relay_ts[space_id] = time.time()

    def record_relay_ts(self, space_id: str) -> None:
        """Bump the local high-water mark for ``space_id``.

        Called by the federation service after a successful own-node
        post-relay. Pairs with :meth:`apply_relay` (which records the
        timestamp on inbound NODE_RELAY) so partition-catchup math
        sees both inbound and locally-originated traffic.
        """
        if space_id:
            self._local_last_relay_ts[space_id] = time.time()

    async def apply_partition_catchup(
        self,
        from_node_id: str,
        last_relay_ts: dict,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> list[dict]:
        """Inbound ``NODE_PARTITION_CATCHUP`` (spec §4.4.6).

        For every space the peer mentions, compare its ``last_relay_ts``
        to ours. If we have newer data the peer must have missed posts
        during the partition (fire-and-discard — they can't be replayed).
        Reply with one ``NODE_PARTITION_GAP`` per affected space so the
        peer can surface the banner to its SH subscribers.

        Returns the list of gap descriptors (also useful for tests).
        """
        if not isinstance(last_relay_ts, dict):
            return []
        gaps: list[dict] = []
        for space_id, peer_ts_raw in last_relay_ts.items():
            try:
                peer_ts = float(peer_ts_raw)
            except TypeError, ValueError:
                continue
            local_ts = self._local_last_relay_ts.get(str(space_id), 0.0)
            if local_ts > peer_ts:
                gaps.append(
                    {
                        "space_id": str(space_id),
                        "gap_start": peer_ts,
                        "gap_end": local_ts,
                    }
                )
        if not gaps or not self._enabled:
            return gaps
        peer_url = await self._lookup_node_url(from_node_id)
        if not peer_url:
            return gaps
        for gap in gaps:
            try:
                await self._post_to_peer(
                    peer_url,
                    NODE_PARTITION_GAP,
                    gap,
                    session=session,
                )
            except Exception as exc:
                log.debug(
                    "cluster: NODE_PARTITION_GAP to %s failed: %s",
                    peer_url,
                    exc,
                )
        return gaps

    async def apply_partition_gap(self, payload: dict) -> None:
        """Inbound ``NODE_PARTITION_GAP``.

        Records the gap so the federation service can drain it via
        :meth:`pending_partition_gaps` and emit ``SPACE_PARTITION_GAP``
        WS frames to subscribed SH instances.
        """
        space_id = str(payload.get("space_id") or "")
        if not space_id:
            return
        try:
            gap_start = float(payload.get("gap_start") or 0.0)
            gap_end = float(payload.get("gap_end") or 0.0)
        except TypeError, ValueError:
            return
        if gap_end <= gap_start:
            return
        self._partition_gaps[space_id] = {
            "space_id": space_id,
            "gap_start": gap_start,
            "gap_end": gap_end,
        }

    def pending_partition_gaps(self) -> list[dict]:
        """Drain + return all ``NODE_PARTITION_GAP`` messages received."""
        gaps = list(self._partition_gaps.values())
        self._partition_gaps.clear()
        return gaps

    async def announce_partition_catchup(
        self,
        peer_url: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Send ``NODE_PARTITION_CATCHUP`` to a peer that just came back.

        Triggered by the heartbeat loop on offline → online transition.
        Broadcasts our current ``_local_last_relay_ts`` per space so the
        peer can compute gaps and reply with ``NODE_PARTITION_GAP``.
        """
        if not self._enabled or not peer_url:
            return
        snapshot = {sid: ts for sid, ts in self._local_last_relay_ts.items()}
        try:
            await self._post_to_peer(
                peer_url,
                NODE_PARTITION_CATCHUP,
                {"last_relay_ts": snapshot},
                session=session,
            )
        except Exception as exc:
            log.debug(
                "cluster: NODE_PARTITION_CATCHUP to %s failed: %s",
                peer_url,
                exc,
            )

    async def _lookup_node_url(self, node_id: str) -> str:
        if not node_id:
            return ""
        nodes = await self._repo.list_nodes()
        for n in nodes:
            if n.node_id == node_id:
                return n.url
        return ""

    # ─── Internals ────────────────────────────────────────────────────

    async def _reannounce_loop(self) -> None:
        """Periodically HELLO every configured peer, until stopped.

        A HELLO must reach the peer for it to learn us — but the startup
        announce is lost if the peer is not yet listening, and a reply-HELLO
        on first contact can hit the same cold-start window (a sibling alloc
        that has printed "Running" but is not yet accepting). Crucially, once
        WE learn a peer (from ITS hello) we cannot tell whether IT learned US,
        so filtering on "peers we don't know" would stop too early and leave
        the link one-directional — exactly the deadlock this fixes. So HELLO
        ALL configured peers every tick: idempotent (:meth:`handle_hello`
        upserts and only replies on genuinely-first contact, so no ping-pong),
        cheap for a handful of allocs, and it also re-registers us after a peer
        restarts or a transient partition. Fail-soft per peer.
        """
        while not self._stop.is_set():
            for peer_url in self._peers:
                if not peer_url or peer_url == self._self_url:
                    continue
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
                except Exception as exc:
                    log.debug(
                        "cluster: re-announce HELLO to %s failed: %s",
                        peer_url,
                        exc,
                    )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=SYNC_RETRY_DELAY_S,
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _announce_to_peers(self) -> None:
        msg = {
            "node_id": self._node_id,
            "url": self._self_url,
            "public_key": self._own_pk_hex,
        }
        for peer_url in self._peers:
            if peer_url == self._self_url:
                continue
            try:
                await self._post_to_peer(peer_url, NODE_HELLO, msg, session=None)
            except Exception as exc:
                log.debug("cluster: NODE_HELLO to %s failed: %s", peer_url, exc)

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=HEARTBEAT_INTERVAL_S,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                rows = await self._repo.list_nodes()
                for r in rows:
                    if r.status == "offline":
                        # Probe offline peers too — coming back triggers
                        # a partition-catchup handshake (spec §4.4.6).
                        if await self._ping_peer(r.url):
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
                                ),
                            )
                            await self.announce_partition_catchup(r.url)
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
                                    "connected_clients": self._own_connected_clients(),
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
            "ts": int(self._wall_clock()),
            "nonce": b64url_encode(secrets.token_bytes(CLUSTER_NONCE_BYTES)),
            "sig_suite": CLUSTER_SIG_SUITE_ED25519,
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
                allow_redirects=False,
                data=canonical,
                headers={
                    "Content-Type": "application/json",
                    "X-Node-Signature": sig,
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                # >= 300: a redirect is not followed, so it is not a sync.
                if resp.status >= 300:
                    raise RuntimeError(f"peer {peer_url} returned {resp.status}")
        finally:
            if own_session:
                await active.close()

    async def _ping_peer(self, peer_url: str) -> bool:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"{peer_url.rstrip('/')}/cluster/health",
                    allow_redirects=False,
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


def _key_source(public_key: str, own_key_lower: str) -> str:
    """How a peer row is trusted: ``own`` (our identity key — the shared
    seed), ``pinned`` (another key: admin-pinned or grandfathered TOFU) or
    ``none`` (no key; only our own key can verify its frames)."""
    key = public_key.lower()
    if not key:
        return "none"
    if own_key_lower and key == own_key_lower:
        return "own"
    return "pinned"


def _frame_digest(raw: bytes) -> bytes:
    """BLAKE2b-256 of a frame's raw signed bytes — the replay-cache key.

    The signature covers exactly these bytes, so any re-encoding fails
    verification; the digest needs no canonicalisation.
    """
    return hashlib.blake2b(raw, digest_size=32).digest()


def _now_iso() -> str:
    """UTC "now" in the naive SQLite ``datetime('now')`` shape.

    ``cluster_nodes.last_seen`` sits beside ``added_at`` (SQL
    ``DEFAULT (datetime('now'))``) — both UTC, so they must share the
    same on-disk shape or an admin-UI reader that assumes one breaks on
    the other. A tz-aware ``isoformat()`` value used to slip in here
    while ``added_at`` stayed naive; naive-format both to match.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
        "icon_url": s.icon_url,
        "min_age": s.min_age,
        "category": normalize_category(s.category),
        # Both directory dials travel with the gossip for the same reason
        # ``withdrawn`` does: without them a peer sync would rebuild the row
        # with the fail-closed defaults, mislabelling an open space as
        # invite-only and silently making a readable space unreadable.
        "join_mode": normalize_join_mode(s.join_mode),
        "allow_subscribers": s.allow_subscribers,
        "accent_color": s.accent_color,
        "primary_color": s.primary_color,
        "status": s.status,
        "subscriber_count": s.subscriber_count,
        "posts_per_week": s.posts_per_week,
        "published_at": s.published_at,
        # The TOFU-pinned space authority key travels too. Omitting it made
        # ``_wire_to_space`` rebuild the row with the dataclass default ("")
        # and ``upsert_space`` clear the pin, so any authenticated cluster
        # peer's NODE_SYNC_SPACE downgraded the space to owner-only relay.
        # ``upsert_space`` now refuses to clear a pin in SQL as well.
        "identity_public_key": s.identity_public_key,
        # v_44 — the owner's cert for that key travels with it; a receiving
        # node re-pins only after verifying it (``apply_sync_space``).
        "authority_cert": s.authority_cert,
        # …and how often this node re-pinned it: followers compare it to
        # decide whether a listing's pin is newer than theirs, so a node
        # must never serve a seq lower than its peer did (max-merged in
        # ``apply_sync_space``).
        "authority_rotation_seq": s.authority_rotation_seq,
        # Owner withdrawal travels with the gossip: without it, the next
        # peer sync would silently un-withdraw a space the owner delisted
        # (the same reason ``status='banned'`` has its ban-wins rule).
        "withdrawn": s.withdrawn,
    }


def _wire_to_space(d: dict) -> GlobalSpace:
    return GlobalSpace(
        space_id=str(d["space_id"]),
        owning_instance=str(d.get("owning_instance") or ""),
        name=str(d.get("name") or ""),
        description=d.get("description"),
        about_markdown=d.get("about_markdown"),
        cover_url=d.get("cover_url"),
        icon_url=d.get("icon_url"),
        min_age=int(d.get("min_age") or 0),
        category=normalize_category(d.get("category")),
        join_mode=normalize_join_mode(d.get("join_mode")),
        allow_subscribers=bool(d.get("allow_subscribers") or False),
        accent_color=str(d.get("accent_color") or "#6366f1"),
        primary_color=str(d.get("primary_color") or "#6366f1"),
        status=str(d.get("status") or "pending"),
        subscriber_count=int(d.get("subscriber_count") or 0),
        posts_per_week=float(d.get("posts_per_week") or 0.0),
        published_at=str(d.get("published_at") or ""),
        identity_public_key=str(d.get("identity_public_key") or ""),
        authority_cert=(
            d["authority_cert"] if isinstance(d.get("authority_cert"), dict) else None
        ),
        authority_rotation_seq=_wire_rotation_seq(d.get("authority_rotation_seq")),
        withdrawn=bool(d.get("withdrawn") or False),
    )


def _wire_rotation_seq(raw: object) -> int:
    """A gossiped ``authority_rotation_seq``; anything malformed reads as 0
    (which the max-merge ignores)."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if 0 <= raw <= MAX_AUTHORITY_KEY_EPOCH else 0


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

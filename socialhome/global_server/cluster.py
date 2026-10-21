"""GFS cluster coordination (spec §24.10).

Symmetric peer-to-peer — no primary/leader. Every node runs the same
code, shares ``client_instances`` + ``global_spaces`` registries via
``NODE_SYNC_*`` messages, and tells siblings about freshly queued envelopes,
owner notices and broadcast relay items via the coalesced
``NODE_DRAIN_HINT`` (``NODE_RELAY`` is a legacy frame nothing sends).
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
import heapq
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

import aiohttp

from ..authority_cert import MAX_AUTHORITY_KEY_EPOCH
from ..capabilities_sig import sign_capabilities
from ..crypto import (
    b64url_decode,
    b64url_encode,
    is_valid_ed25519_public_key,
    sign_ed25519,
    verify_ed25519,
)
from ..domain.space import normalize_category, normalize_join_mode
from ..domain.space_place import PlacePin, parse_place_pin_wire, place_pin_wire
from ..domain.subscriber_accepts import parse_accepts
from ..gfs_proof import sign_possession_proof
from .cluster_relay import (
    RELAY_FORWARD_MAX_FRAME_BYTES,
    RELAY_FORWARD_MAX_PENDING,
    RELAY_FORWARD_MAX_PENDING_BYTES,
    RELAY_FORWARD_MAX_PER_FRAME,
    RELAY_FORWARD_WARN_INTERVAL_S,
    ForwardedRelay,
    SettleCallback,
    held_from_replies,
    wire_size,
)
from .domain import ClientInstance, ClusterNode, GfsFraudReport, GlobalSpace
from .envelope_relay import ENVELOPE_INSTANCE_ID_RE
from .federation import certified_authority_repin
from .peer_url import normalized_peer_url
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
#: node (signature checked against the key that node is trusted under). Never keyed
#: on a claimed id: an unverified request must not spend a real node's budget.
CLUSTER_RATE_LIMIT_PER_MIN: int = 60

#: ``/cluster/sync`` requests per minute per source address that did NOT prove
#: a member: malformed bodies, unknown suites, stale timestamps, wrong
#: recipients, unknown or unapproved senders, key mismatches, bad signatures
#: and replays. Once spent, the address is SHED: a frame from it is still
#: parsed and checked (cheap, no crypto) against the roster, and every frame
#: that does not name an approved node is answered 429 without a signature
#: verify. Only a frame naming an approved node is verified — see
#: :data:`CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN` — so junk sent from a
#: member's address can never lock the member out. Genuine peers never touch
#: this budget, so 30/min is far above real use.
CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN: int = 30

#: Failed signature verifies per minute per (approved node, source address),
#: counted only for frames from a SHED address (see above). Such a frame
#: names an approved node, so it is verified: one that verifies is the
#: member's own traffic and is accepted (then capped by
#: :data:`CLUSTER_RATE_LIMIT_PER_MIN`); one that fails spends this budget for
#: that node AND that address only. Once spent, further frames naming that
#: node from that address are answered 429 without a verify. Keyed on the
#: pair, not the node: under a ``trusted_proxies`` that believes
#: ``X-Forwarded-For`` from a whole private network, any host there can claim
#: any address — forgeries "from" other addresses must not spend the budget
#: the member's own address uses. Residual: an attacker who can send from
#: the member's own address AND forge more than this per minute delays that
#: member until the window slides (list only the real proxy in
#: ``trusted_proxies``).
CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN: int = 30

#: Global ceiling on failed verifies per approved node per minute, across
#: every shed address — the CPU bound the per-pair budget alone cannot give
#: (an attacker can claim many addresses). Far above
#: :data:`CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN` so forgeries from a few
#: hosts never reach it; at ~50 µs per Ed25519 verify it costs well under a
#: tenth of a CPU-second per node per minute.
CLUSTER_FAILED_VERIFY_NODE_CEILING_PER_MIN: int = 1200

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

#: Seconds past ``ts + CLUSTER_TS_SKEW_S`` an accepted frame's digest is kept.
#: ``ts`` is whole seconds and the wall clock is not, so the entry must
#: outlive the last instant the frame is still fresh.
CLUSTER_REPLAY_SLACK_S: int = 1

#: Longest an accepted frame's digest can be kept: a frame dated
#: ``now + CLUSTER_TS_SKEW_S`` stays fresh for ``2 × CLUSTER_TS_SKEW_S``.
#: Used to size the cache (see :class:`ClusterReplayCache` for the expiry).
CLUSTER_REPLAY_TTL_S: float = float(2 * CLUSTER_TS_SKEW_S + CLUSTER_REPLAY_SLACK_S)

#: Most frames one verified node can have accepted within one replay TTL:
#: :data:`CLUSTER_RATE_LIMIT_PER_MIN` caps it at 60 in any 60 s window, so
#: at most ``60 × ceil(TTL / 60)`` = 660 inside a 601 s TTL. Only ACCEPTED
#: frames are recorded, so an honest node never reaches it.
CLUSTER_REPLAY_MAX_PER_NODE: int = CLUSTER_RATE_LIMIT_PER_MIN * math.ceil(
    CLUSTER_REPLAY_TTL_S / 60
)

#: Most shared-seed siblings on the roster: an own-key ``NODE_HELLO`` (a
#: sibling's first contact) creates no row while this many sibling rows —
#: rows with no operator approval, holding our own key — exist (403
#: ``cluster_full``). Operator-approved rows do not count (each one is an
#: explicit admin act), nor do rows that are no member at all.
CLUSTER_MAX_NODES: int = 32

#: A shared-seed sibling's row unseen this long is dropped by the heartbeat
#: loop: every Nomad allocation is a new node id under the shared seed, so
#: without it the roster fills with dead allocations and the next sibling
#: gets ``cluster_full`` for good. Ten heartbeat intervals — a live sibling
#: is refreshed every interval (our ping, its heartbeat or HELLO), and a
#: dropped one that comes back simply rejoins with its next HELLO.
#: Operator-approved rows are never dropped.
CLUSTER_STALE_SIBLING_S: int = 10 * HEARTBEAT_INTERVAL_S

#: How long a sibling's reported ``instance_id`` mismatch stays in force
#: without being re-reported. A live sibling re-reports on every heartbeat;
#: one that went away (or was fixed and restarted under another node id)
#: stops failing ``/healthz`` after this — the stale-sibling horizon.
CLUSTER_IDENTITY_MISMATCH_TTL_S: int = CLUSTER_STALE_SIBLING_S

#: Replay-cache capacity across all nodes: ``None`` — the per-node share
#: (:data:`CLUSTER_REPLAY_MAX_PER_NODE`, ~110 KB) is the binding bound. Only
#: frames that verified under a member's key are recorded, so the nodes with
#: live entries are the members of the last TTL: the operator-approved rows
#: plus at most :data:`CLUSTER_MAX_NODES` siblings at a time (a stale one is
#: dropped after :data:`CLUSTER_STALE_SIBLING_S`, so a few generations of
#: them within one TTL). A global cap would refuse honest frames from a
#: large approved roster for no gain; a node at its share is REFUSED
#: (503 ``replay_cache_full``), never let in by evicting a live digest (see
#: :class:`ClusterReplayCache`).
CLUSTER_REPLAY_MAX_ENTRIES: int | None = None

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
#: Legacy: nothing sends it any more (it never had a production caller).
#: Broadcast relays ride ``NODE_DRAIN_HINT.relays`` instead — see
#: :mod:`.cluster_relay`. The receiver still accepts it from an older node.
NODE_RELAY = "NODE_RELAY"
NODE_POLICY_PUSH = "NODE_POLICY_PUSH"
#: Spec §24.10.3 / §4.4.6 — partition-healing pair. ``CATCHUP`` is sent
#: by a node that just came back online; the receiver replies with one
#: ``GAP`` per space where its own ``last_relay_ts`` is newer than the
#: sender's (fire-and-discard posts can't be replayed; subscribers see
#: a banner instead).
NODE_PARTITION_CATCHUP = "NODE_PARTITION_CATCHUP"
NODE_PARTITION_GAP = "NODE_PARTITION_GAP"
#: "I just queued something for these households" — sent after an envelope or
#: relay item lands in the shared ``gfs_envelope_queue``. The households'
#: sockets may live on a sibling node (each node only knows its own), so every
#: receiver drains the listed ids it holds a socket for. Payload:
#: ``{"instances": [<instance id>, ...]}`` — ids only, never content.
#:
#: The same frame carries the optional ``notices`` list: ``new_subscriber``
#: notices whose space OWNER had no socket on the sending node, as
#: ``{owner, space_id, subscriber, reason, accepts}`` — ids and labels the
#: sender read from the shared DB, never key material. The node holding the
#: owner's socket rebuilds the frame from the shared DB
#: (``GfsFederationService.deliver_forwarded_notice``). Riding the drain hint
#: (rather than a frame of its own) keeps it inside the same coalesced send,
#: so it costs a peer's per-minute sync budget nothing extra; an older node
#: ignores the key, which is exactly how it behaved before.
#:
#: And the optional ``relays`` list: broadcast relay items this node accepted
#: (``ForwardedRelay.to_wire``) for every other node to push to the sockets it
#: holds — one copy per peer, however many subscribers. A newer receiver
#: answers ``{"status": "ok", "held": {audience: [instance id, ...]}}`` for
#: the items that may need an offline copy; an older one ignores the key and
#: answers without ``held``. See :mod:`.cluster_relay`.
NODE_DRAIN_HINT = "NODE_DRAIN_HINT"

#: How long :meth:`ClusterService.hint_drain` gathers ids before it sends one
#: ``NODE_DRAIN_HINT``. A burst (a catch-up backfill, a space fan-out to many
#: offline subscribers) becomes one frame per peer instead of one per row.
DRAIN_HINT_DELAY_S: float = 0.5

#: Minimum seconds between two ``NODE_DRAIN_HINT`` frames to one peer, so a
#: steady stream of relays, ids and notices stays at <= 40 frames a minute —
#: inside the receiver's :data:`CLUSTER_RATE_LIMIT_PER_MIN` (60) with room for
#: heartbeats and syncs. Whatever arrives meanwhile rides the next frame, so
#: busier traffic means fuller frames, not more of them. Idle, the first frame
#: still goes after :data:`DRAIN_HINT_DELAY_S`.
DRAIN_HINT_MIN_GAP_S: float = 1.5

#: Largest reply body read from a peer's ``NODE_DRAIN_HINT`` answer (its
#: ``held`` lists: ~35 bytes an instance id).
DRAIN_HINT_REPLY_MAX_BYTES: int = 8 * 1024 * 1024

#: Max instance ids in one ``NODE_DRAIN_HINT`` frame (a bigger batch is split),
#: and the most a receiver reads from one frame.
DRAIN_HINT_MAX_IDS: int = 256

#: Most callers waiting at once for the next ``NODE_DRAIN_HINT`` frame
#: (:meth:`ClusterService.next_hint_round`). Past it a caller does not wait.
FLUSH_WAITERS_MAX: int = 1024

#: Max owner notices in one ``NODE_DRAIN_HINT`` frame, and the most a receiver
#: reads from one. ~250 bytes each, so a full frame stays near 64 KiB.
SUBSCRIBER_NOTICE_MAX_PER_FRAME: int = 256

#: Max owner notices waiting for the next flush. Past it a notice is dropped
#: (WARNING once per flush window): the owner's 5b-c reconcile and the
#: follower's next reconnect still repair the key.
SUBSCRIBER_NOTICE_MAX_PENDING: int = 4096

#: Longest ``space_id`` a forwarded notice may name — the relay routes' own
#: bound on identifier-shaped wire fields.
SUBSCRIBER_NOTICE_SPACE_ID_MAX_LEN: int = 128

#: The ``new_subscriber.reason`` values a forwarded notice may carry (mirrors
#: ``federation.NEW_SUBSCRIBER_REASONS``; the receiver re-checks).
_NOTICE_REASONS: frozenset[str] = frozenset({"subscribe", "reconnect"})


@dataclass(slots=True, frozen=True)
class SubscriberNotice:
    """One forwarded ``new_subscriber`` notice, validated off the wire."""

    owner: str
    space_id: str
    subscriber: str
    reason: str
    accepts: tuple[str, ...]


#: ``GfsFederationService.deliver_forwarded_notice`` — set by
#: :meth:`ClusterService.attach_subscriber_notice`.
NoticeDelivery = Callable[..., Awaitable[bool]]


#: Longest ``node_id`` the admin API accepts for a peer.
CLUSTER_NODE_ID_MAX_LEN: int = 128

_HEX_ED25519_KEY = re.compile(r"[0-9a-f]{64}")

#: Characters a peer ``node_id`` may use: what config-set ids look like in
#: practice (``gfs-node-0``, UUIDs, ``host:port``, URL-shaped ids), and
#: nothing that renders deceptively in the admin UI or a log line —
#: no whitespace, control, bidi or other non-ASCII characters.
_NODE_ID_RE = re.compile(r"[A-Za-z0-9._:/-]+")


class InvalidClusterPeer(ValueError):
    """Admin add-peer input is malformed; ``code`` is the API error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ClusterPeerKeyMismatch(Exception):
    """Admin add-peer named a node already approved under a different key."""


def _validated_peer(
    node_id: object, url: object, public_key: object, *, own_node_id: str
) -> tuple[str, str, str]:
    """Normalise + validate an admin add-peer request, or raise
    :class:`InvalidClusterPeer`. Returns ``(node_id, url, public_key)``."""
    if not isinstance(node_id, str):
        raise InvalidClusterPeer("invalid_node_id")
    node_id = node_id.strip()
    if len(node_id) > CLUSTER_NODE_ID_MAX_LEN or not _NODE_ID_RE.fullmatch(node_id):
        raise InvalidClusterPeer("invalid_node_id")
    if node_id == own_node_id:
        raise InvalidClusterPeer("node_id_is_self")
    url = normalized_peer_url(url)
    if not url:
        raise InvalidClusterPeer("invalid_url")
    if not isinstance(public_key, str):
        raise InvalidClusterPeer("invalid_public_key")
    key = public_key.strip().lower()
    # A small-order key would let anyone forge this node's frames.
    if not _HEX_ED25519_KEY.fullmatch(key) or not is_valid_ed25519_public_key(
        bytes.fromhex(key)
    ):
        raise InvalidClusterPeer("invalid_public_key")
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


def is_member(row: ClusterNode, own_key: str) -> bool:
    """Whether the ``cluster_nodes`` *row* is a cluster member (spec §24.10).

    The one membership rule, for both directions: :func:`authorize_frame`
    takes frames only from a member row, and every outbound path —
    fan-out, heartbeats, partition catch-up, the signaling pick and the
    public ``/cluster/health`` list — targets member rows only. A row is a
    member if and only if

    * an operator approved a key for it (``approved_key`` non-empty —
      written by admin add-peer alone, never by old-version code), or
    * its ``public_key`` is our OWN identity key: a shared-seed sibling.
      Every writer of that value proved the seed first — this build's
      :meth:`ClusterService.handle_hello` inserts or reclaims a row with
      our key only after a HELLO verified under it, and an old-version
      node sharing the DB writes ``public_key`` only as the key the HELLO
      verified under (trust-on-first-use checks the signature against the
      carried key), so writing OUR key takes a signature by our seed. Old
      code's other writes copy a row's own snapshot back or write ``''``.

    Everything else — a TOFU row an old-version node inserted for some
    self-signed HELLO, a sibling row such a node rewrote to an attacker's
    key, a keyless row from the old add-peer — is not a member: it gets
    no traffic and its frames are refused until a HELLO under our key
    reclaims it, or an operator approves it.
    """
    if row.approved_key:
        return True
    own = own_key.lower()
    return bool(own) and row.public_key.lower() == own


def member_url(row: ClusterNode) -> str:
    """The base URL outbound traffic to *row* goes to.

    An approved node: ``approved_url``, the URL the operator approved with
    its key — written only by admin add-peer. Never ``url``: an
    old-version node sharing the DB rewrites ``url`` on any HELLO it
    TOFU-verifies, so it would let an outsider redirect the node's traffic.
    A shared-seed sibling: ``url``, which its own HELLO or heartbeat under our
    key refreshes (its alloc may come back on a new port). An old-version
    node can rewrite ``url`` only together with ``public_key`` (the key the
    HELLO verified under — not our own without our seed), which makes the
    row no member (:func:`is_member`), so no traffic follows it.
    """
    return row.approved_url if row.approved_key else row.url


def authorize_frame(
    *,
    msg_type: str,
    from_node: str,
    carried_key: str,
    row: ClusterNode | None,
    own_key: str,
) -> FrameVerdict:
    """Decide which key a ``/cluster/sync`` frame must verify under (§24.10).

    Pure: no I/O, no logging. A node is a cluster member if and only if its
    frames verify under a key this GFS already holds:

    * our OWN identity key — the shared seed; an operator who gave a node
      the seed approved it; or
    * the key an operator approved for the node — ``approved_key`` on its
      ``cluster_nodes`` *row*, written only by admin add-peer
      (``POST /admin/api/cluster/peers``).

    The row's ``public_key`` is never consulted: older builds, and
    old-version nodes sharing the DB during a rolling upgrade, write it by
    trust-on-first-use.

    ``NODE_HELLO`` names the key it is signed under (``carried_key``):

    * the node's approved key → member; any other key → ``key_mismatch``
      (an approval never moves in-band, not even to our own key — rotation
      is delete then re-add);
    * no row, or no approved key → member only under our own key, else
      ``unapproved_node`` (the caller writes nothing).

    Every other frame needs a row (``unknown_node`` otherwise) that is a
    member (:func:`is_member`; ``unapproved_node`` otherwise) and verifies
    under its approved key, or under our own key when it has none (a
    shared-seed sibling).
    """
    own = own_key.lower()
    approved = (row.approved_key if row is not None else "").lower()
    if msg_type == NODE_HELLO:
        carried = carried_key.lower()
        if approved:
            if carried == approved:
                return FrameVerdict(verify_key=approved)
            return FrameVerdict(error="key_mismatch")
        if own and carried == own:
            return FrameVerdict(verify_key=own)
        return FrameVerdict(error="unapproved_node")
    if row is None:
        return FrameVerdict(error="unknown_node")
    if not is_member(row, own):
        return FrameVerdict(error="unapproved_node")
    return FrameVerdict(verify_key=approved or own)


class ClusterReplayCache:
    """Digests of accepted ``/cluster/sync`` frames, keyed on the frame's
    own signed ``ts`` and expired on the WALL clock — the clock the
    freshness check reads, so the two windows can never drift apart.

    An entry is kept until wall-clock ``ts + CLUSTER_TS_SKEW_S + slack``,
    the first instant its frame is stale. An expired entry raises a
    ``floor``: a frame whose ``ts`` is at or below it counts as seen, so a
    frame the cache no longer holds can never be accepted again, even if
    the wall clock later steps back (making it "fresh" once more). Under a
    steady clock the floor trails the freshness window and refuses nothing
    a fresh frame could have; it is built from peers' ``ts`` values, not
    our clock, so a bogus forward jump of our clock does not partition the
    cluster once it is corrected.

    The cache never evicts a live entry. Evicting one would have to raise
    the floor to its ``ts`` — and a member dating its frames ``now + 300``
    could then push the floor past every honest frame. Instead, when the
    cache holds ``cap`` entries (``None``: no total cap), or the sending
    node holds ``per_node_cap``, :meth:`record` refuses the NEW frame; the caller answers 503 and the
    sender retries. A node is capped at its own share, so one node cannot
    crowd out another. In-memory, per process — the boot floor covers a
    restart.
    """

    __slots__ = (
        "_cap",
        "_entries",
        "_expiry_heap",
        "_floor",
        "_per_node",
        "_per_node_cap",
    )

    def __init__(self, *, cap: int | None, per_node_cap: int) -> None:
        self._cap = cap
        self._per_node_cap = per_node_cap
        #: digest → sending node id.
        self._entries: dict[bytes, str] = {}
        #: node id → live entries.
        self._per_node: dict[str, int] = {}
        self._expiry_heap: list[tuple[int, bytes]] = []
        self._floor: int | None = None

    def seen(self, digest: bytes, ts: int, *, now: float) -> bool:
        """Whether a frame with *digest* and signed *ts* may be a replay."""
        self._expire(now)
        if self._floor is not None and ts <= self._floor:
            return True
        return digest in self._entries

    def record(self, digest: bytes, ts: int, node_id: str, *, now: float) -> bool:
        """Remember an accepted frame until its ``ts`` window closes.

        Returns ``False`` — the frame must be refused — when there is no
        room: the cache or *node_id*'s share is full of live entries.
        """
        self._expire(now)
        if digest in self._entries:
            return True
        if (
            self._cap is not None and len(self._entries) >= self._cap
        ) or self._per_node.get(node_id, 0) >= self._per_node_cap:
            return False
        self._entries[digest] = node_id
        self._per_node[node_id] = self._per_node.get(node_id, 0) + 1
        heapq.heappush(self._expiry_heap, (ts, digest))
        return True

    def _expire(self, now: float) -> None:
        horizon = now - CLUSTER_TS_SKEW_S - CLUSTER_REPLAY_SLACK_S
        while self._expiry_heap and self._expiry_heap[0][0] <= horizon:
            ts, digest = heapq.heappop(self._expiry_heap)
            node_id = self._entries.pop(digest, None)
            if node_id is not None:
                left = self._per_node[node_id] - 1
                if left:
                    self._per_node[node_id] = left
                else:
                    del self._per_node[node_id]
            self._floor = ts if self._floor is None else max(self._floor, ts)

    @property
    def floor(self) -> int | None:
        """Highest ``ts`` the cache has forgotten (``None``: nothing yet)."""
        return self._floor

    def __len__(self) -> int:
        return len(self._entries)


class ClusterService:
    """Spec-shape :class:`ClusterService`.

    All nodes are equal — no leader election or consensus protocol
    (spec §28431). ``list_nodes`` works whether cluster mode is enabled
    or not.
    """

    __slots__ = (
        "_active_sync_count",
        "_admin_repo",
        "_announce_task",
        "_clock",
        "_connected_clients",
        "_drain_cb",
        "_drain_hint_pending",
        "_drain_hint_task",
        "_dup_url_warned",
        "_duplicate_node_urls",
        "_enabled",
        "_fail_counts",
        "_fed_repo",
        "_flush_waiters",
        "_heartbeat_task",
        "_hint_drains",
        "_hint_rerun",
        "_hint_sent_at",
        "_identity_mismatches",
        "_identity_transitional",
        "_instance_id",
        "_instance_id_aliases",
        "_local_last_relay_ts",
        "_node_id",
        "_notice_cb",
        "_notice_dropped",
        "_notice_pending",
        "_notice_tasks",
        "_own_pk_hex",
        "_partition_gaps",
        "_peers",
        "_process_start",
        "_relay_cb",
        "_relay_dropped",
        "_relay_pending",
        "_relay_pending_bytes",
        "_relay_warned_at",
        "_repo",
        "_seen_frames",
        "_seen_relays",
        "_self_url",
        "_signing_key",
        "_stop",
        "_sync_failed_verify_limiter",
        "_sync_failed_verify_node_limiter",
        "_sync_node_limiter",
        "_sync_unverified_limiter",
        "_wall_clock",
        "_ws_registry",
    )

    def __init__(
        self,
        repo: AbstractClusterRepo,
        *,
        admin_repo: AbstractGfsAdminRepo | None = None,
        fed_repo: AbstractGfsFederationRepo | None = None,
        node_id: str = "",
        instance_id: str = "",
        instance_id_aliases: tuple[str, ...] = (),
        self_url: str = "",
        peers: tuple[str, ...] = (),
        signing_key: bytes = b"",
        own_public_key_hex: str = "",
        enabled: bool = False,
        ws_registry: GfsWebSocketRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo = repo
        self._admin_repo = admin_repo
        self._fed_repo = fed_repo
        self._node_id = node_id
        #: This GFS's PUBLIC id (``[server] instance_id``). Every node holding
        #: our identity key must serve the same one — households pin it — so
        #: it rides HELLO + heartbeat and a sibling reporting another one is
        #: an operator error (:meth:`_note_sibling_identity`).
        self._instance_id = instance_id
        #: Our transitional former ids (``[server] instance_id_aliases``).
        self._instance_id_aliases = frozenset(instance_id_aliases)
        #: Sibling node id → (the instance_id it reported, monotonic time of
        #: the report). Only siblings under our OWN key that sent the field.
        #: ``_identity_transitional`` holds siblings whose id differs from ours
        #: but is linked through either side's aliases (a rolling id change).
        self._identity_mismatches: dict[str, tuple[str, float]] = {}
        self._identity_transitional: dict[str, tuple[str, float]] = {}
        #: Advertised URL → monotonic time a node under our key announced OUR
        #: node_id from it — two nodes sharing one ``node_id``.
        self._duplicate_node_urls: dict[str, float] = {}
        self._self_url = self_url
        #: Node ids we already WARNed about for announcing OUR url (a
        #: duplicated ``advertise_url``) — once per node, not per heartbeat.
        self._dup_url_warned: set[str] = set()
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
        #: Accepted-frame digests (see :class:`ClusterReplayCache`).
        self._seen_frames = ClusterReplayCache(
            cap=CLUSTER_REPLAY_MAX_ENTRIES, per_node_cap=CLUSTER_REPLAY_MAX_PER_NODE
        )
        #: Per VERIFIED node id (spec §24.10.4). Capped LRU, like every GFS
        #: limiter, though only proven peers ever get a bucket here.
        self._sync_node_limiter = SlidingWindowCounter(CLUSTER_RATE_LIMIT_PER_MIN)
        #: Per source address, spent only by requests that proved no known
        #: peer (see :data:`CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN`).
        self._sync_unverified_limiter = SlidingWindowCounter(
            CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN
        )
        #: Per (approved node id, source address): failed verifies of frames
        #: from shed addresses (see
        #: :data:`CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN`).
        self._sync_failed_verify_limiter = SlidingWindowCounter(
            CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN
        )
        #: Per approved node id, across addresses — the CPU ceiling (see
        #: :data:`CLUSTER_FAILED_VERIFY_NODE_CEILING_PER_MIN`).
        self._sync_failed_verify_node_limiter = SlidingWindowCounter(
            CLUSTER_FAILED_VERIFY_NODE_CEILING_PER_MIN
        )
        #: Recipient ids waiting for the next ``NODE_DRAIN_HINT`` flush.
        self._drain_hint_pending: set[str] = set()
        #: The one-shot flush task, live while ids are pending.
        self._drain_hint_task: asyncio.Task | None = None
        #: ``GfsEnvelopeRelay.drain``, set by :meth:`attach_drain`.
        self._drain_cb: Callable[[str], Awaitable[int]] | None = None
        #: Running hint-started drain per household, awaited / cancelled in
        #: ``stop``. At most one per household: further hints coalesce.
        self._hint_drains: dict[str, asyncio.Task] = {}
        #: Households hinted again while their drain ran — drained once more.
        self._hint_rerun: set[str] = set()
        #: Owner notices waiting for the next ``NODE_DRAIN_HINT`` flush, keyed
        #: by ``(owner, space_id, subscriber)`` → ``(reason, accepts)``.
        self._notice_pending: dict[
            tuple[str, str, str], tuple[str, tuple[str, ...]]
        ] = {}
        #: Notices dropped at the pending cap since the last flush.
        self._notice_dropped = 0
        #: ``deliver_forwarded_notice``, set by :meth:`attach_subscriber_notice`.
        self._notice_cb: NoticeDelivery | None = None
        #: Running inbound notice deliveries, awaited / cancelled in ``stop``.
        self._notice_tasks: set[asyncio.Task] = set()
        #: Broadcast relay items waiting for the next ``NODE_DRAIN_HINT``
        #: flush: ``(item, its wire dict, its wire size, settle callback)``.
        self._relay_pending: list[
            tuple[ForwardedRelay, dict, int, SettleCallback | None]
        ] = []
        self._relay_pending_bytes = 0
        #: Callers waiting for the next ``NODE_DRAIN_HINT`` frame to go out
        #: (:meth:`next_hint_round`), at most :data:`FLUSH_WAITERS_MAX`.
        self._flush_waiters: list[asyncio.Future[None]] = []
        #: Items refused at the pending caps since the last WARNING.
        self._relay_dropped = 0
        self._relay_warned_at = -math.inf
        #: ``ClusterRelayFanOut.apply``, set by :meth:`attach_forwarded_relays`.
        self._relay_cb: (
            Callable[[list[ForwardedRelay]], Awaitable[dict[str, list[str]]]] | None
        ) = None
        #: Monotonic time the last ``NODE_DRAIN_HINT`` frame went out.
        self._hint_sent_at = -math.inf

    # ─── /cluster/sync budgets ───────────────────────────────────────

    def sync_source_exhausted(self, client_ip: str) -> bool:
        """Whether *client_ip* has spent its unverified budget.

        Read-only — checked first. A shed address still has its frames
        parsed and matched against the roster (no crypto); only a frame
        naming an approved node is then verified.
        """
        return self._sync_unverified_limiter.exhausted(client_ip, now=self._clock())

    def charge_unverified_sync(self, client_ip: str) -> bool:
        """Spend one unit of *client_ip*'s unverified budget.

        Called for every request that did not prove a member. Returns
        whether the request is still within budget (a request already being
        rejected ignores it).
        """
        return self._sync_unverified_limiter.allow(client_ip, now=self._clock())

    def failed_verify_exhausted(self, node_id: str, client_ip: str) -> bool:
        """Whether forged frames from shed addresses have spent the
        failed-verify budget of (*node_id*, *client_ip*) or *node_id*'s
        global ceiling (read-only, checked before a verify)."""
        now = self._clock()
        return self._sync_failed_verify_limiter.exhausted(
            _node_address_key(node_id, client_ip), now=now
        ) or self._sync_failed_verify_node_limiter.exhausted(node_id, now=now)

    def charge_failed_verify(self, node_id: str, client_ip: str) -> None:
        """Spend one unit of the (*node_id*, *client_ip*) failed-verify
        budget and of *node_id*'s ceiling: a frame from a shed address named
        the node but did not verify under its key."""
        now = self._clock()
        self._sync_failed_verify_limiter.allow(
            _node_address_key(node_id, client_ip), now=now
        )
        self._sync_failed_verify_node_limiter.allow(node_id, now=now)

    def charge_verified_sync(self, node_id: str) -> bool:
        """Spend one unit of a VERIFIED peer's budget; ``False`` → 429.

        *node_id* must be the id whose trusted key just verified the
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

    def frame_seen(self, raw: bytes, ts: int) -> bool:
        """Whether these exact signed frame bytes (signed *ts*) were already
        accepted — or may have been, and the cache has since forgotten."""
        return self._seen_frames.seen(_frame_digest(raw), ts, now=self._wall_clock())

    def record_frame(self, raw: bytes, ts: int, node_id: str) -> bool:
        """Remember a frame *node_id* sent, so a byte-identical resend is
        refused; ``False`` → no room, refuse the frame (503).

        Called only once the frame passed every check, right before
        dispatch — a rejected frame never poisons the cache. The entry
        lives until wall-clock ``ts + CLUSTER_TS_SKEW_S + slack``: as long
        as *ts* can still pass :meth:`frame_ts_error`.
        """
        return self._seen_frames.record(
            _frame_digest(raw), ts, node_id, now=self._wall_clock()
        )

    def roster_full(self, nodes: list[ClusterNode]) -> bool:
        """Whether *nodes* already hold :data:`CLUSTER_MAX_NODES` shared-seed
        siblings, so an own-key HELLO may not create another row.

        Counts only rows with no approval that hold our own key: an
        approved row is the operator's, and a row that is no member (an
        old-version node's TOFU row) must not lock siblings out.
        """
        return (
            sum(
                1
                for n in nodes
                if n.node_id != self._node_id
                and not n.approved_key
                and is_member(n, self._own_pk_hex)
            )
            >= CLUSTER_MAX_NODES
        )

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
        the persisted identity seed (shared by every node of a shared-seed
        cluster) and never leaves the process.
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

    @property
    def has_signing_key(self) -> bool:
        """Whether this node can sign (capability block, possession proof)."""
        return bool(self._signing_key)

    def sign_possession_proof(self, nonce: str, base_url: str) -> tuple[str, str]:
        """Answer a household's ``POST /gfs/proof`` challenge with the
        identity key (:mod:`socialhome.gfs_proof`).

        Returns ``(proof_sig, proof_sig_suite)``, or ``("", "")`` when no
        signing key is wired — the route then answers ``503`` and ``/gfs/info``
        does not advertise ``proof_of_possession``. Same key as the capability
        block; no new key is minted. The proof binds the KEY (shared by
        every node of a cluster), never this node's instance id.
        """
        if not self._signing_key:
            return "", ""
        return sign_possession_proof(self._signing_key, nonce, base_url)

    # ─── Node registry API ────────────────────────────────────────────

    async def list_nodes(self) -> list[ClusterNode]:
        return await self._repo.list_nodes()

    async def member_peers(self) -> list[ClusterNode]:
        """Every OTHER node that is a cluster member (:func:`is_member`) —
        the only rows outbound traffic may go to."""
        return [
            r
            for r in await self._repo.list_nodes()
            if r.node_id != self._node_id and is_member(r, self._own_pk_hex)
        ]

    async def node_id_for_url(self, url: str) -> str:
        """The node id whose outbound URL is *url* (ours included), or
        ``""`` — members only."""
        if not url:
            return ""
        if url == self._self_url:
            return self._node_id
        for r in await self.member_peers():
            if member_url(r) == url:
                return r.node_id
        return ""

    # ─── Cluster lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        """Announce to seed peers + start the heartbeat loop.

        No-op if cluster mode is disabled.
        """
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

    async def stop(self) -> None:
        self._stop.set()
        for attr in ("_heartbeat_task", "_announce_task", "_drain_hint_task"):
            task = getattr(self, attr)
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except TimeoutError, asyncio.CancelledError:
                    task.cancel()
                except Exception:  # pragma: no cover
                    log.debug("cluster: %s ended with an error", attr, exc_info=True)
                setattr(self, attr, None)
        self._drain_hint_pending.clear()
        self._notice_pending.clear()
        self._hint_rerun.clear()
        # Relay items that never went out: no peer reported a socket for
        # anybody, so the origin queues every offline copy it would have.
        unsent, self._relay_pending = self._relay_pending, []
        self._relay_pending_bytes = 0
        for _item, _wire, _size, on_settled in unsent:
            if on_settled is not None:
                on_settled(frozenset())
        waiters, self._flush_waiters = self._flush_waiters, []
        self._release_flush_waiters(waiters)
        if self._notice_tasks:
            tasks = set(self._notice_tasks)
            _done, still = await asyncio.wait(tasks, timeout=5.0)
            for task in still:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._hint_drains:
            pending = set(self._hint_drains.values())
            _done, still = await asyncio.wait(pending, timeout=5.0)
            for task in still:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def health(self) -> dict:
        """Return this node's cluster status (public ``GET /cluster/health``).

        Lists cluster members only (:func:`is_member`): a row nobody
        approved — an old-version node's TOFU row — is neither a peer nor
        something to advertise. Each peer is ``node_id``, ``status`` and
        ``last_seen`` — never its URL: with ``[cluster] advertise_url`` a
        row holds an internal node address + port, and this page is
        unauthenticated. URLs are admin-only (:meth:`admin_cluster`).
        """
        return {
            "node_id": self._node_id,
            "status": "online" if self._enabled else "single-node",
            "peers": [
                {
                    "node_id": r.node_id,
                    "status": r.status,
                    "last_seen": r.last_seen,
                }
                for r in await self.member_peers()
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
                    "url": member_url(r),
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
                    **(
                        {"public_key": self._own_pk_hex, "key_source": "own"}
                        if is_self
                        else _key_view(r, self._own_pk_hex.lower())
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
            # Our own identity key — what an operator approves for this node on
            # every other node (``POST /admin/api/cluster/peers``).
            "public_key": self._own_pk_hex,
            # Our public id, and every sibling under our key that serves
            # another one (``/healthz`` fails while this is non-empty).
            "instance_id": self._instance_id,
            "instance_id_mismatches": self.identity_mismatches(),
            "instance_id_transitional": self.identity_transitional(),
            "duplicate_node_id_urls": self.duplicate_node_id_urls(),
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
        candidates: list[tuple[int, str, str]] = []
        # Members only — and never our own row: self is added below with
        # the in-memory authoritative count.
        for r in await self.member_peers():
            if r.status == "offline":
                continue
            count = self._active_sync_count.get(
                r.node_id,
                int(r.active_sync_sessions or 0),
            )
            if count >= MAX_SIGNALING_SESSIONS:
                continue
            candidates.append((count, r.node_id, member_url(r)))
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
                "cluster: failed to persist active_sync_sessions for self: %r",
                exc,
                exc_info=True,
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

    # ─── Cross-node envelope-queue drain (NODE_DRAIN_HINT) ────────────

    def attach_drain(self, drain: Callable[[str], Awaitable[int]]) -> None:
        """Register the callback an inbound ``NODE_DRAIN_HINT`` runs per
        locally-connected household (``GfsEnvelopeRelay.drain``). A setter,
        not a constructor argument: the relay hints this service too."""
        self._drain_cb = drain

    def hint_drain(self, instance_id: str) -> None:
        """Note that a row for *instance_id* just landed in the shared queue.

        Synchronous and cheap — called from the enqueue path. Ids are
        coalesced for :data:`DRAIN_HINT_DELAY_S` and sent as one
        fire-and-forget ``NODE_DRAIN_HINT`` per peer. No-op when cluster mode
        is off (a single node drains on hello, there is nobody to tell).
        """
        if not self._enabled:
            return
        self._drain_hint_pending.add(instance_id)
        self._schedule_hint_flush()

    def forward_relay(
        self, item: ForwardedRelay, *, on_settled: SettleCallback | None = None
    ) -> bool:
        """Send a broadcast relay *item* to every other node in the next
        coalesced ``NODE_DRAIN_HINT`` (:mod:`.cluster_relay`).

        Synchronous and cheap — called from the fan-out path. *on_settled*
        runs once the frame went to every member peer, with the ids their
        replies report ``held`` for the item's audience (the empty set if
        none answered, or at shutdown). ``False`` — nothing is sent — when
        cluster mode is off or the pending caps
        (:data:`~.cluster_relay.RELAY_FORWARD_MAX_PENDING`, ``…_BYTES``) are
        reached (WARNING once a minute); the caller then delivers as on a
        single node."""
        if not self._enabled:
            return False
        wire = item.to_wire()
        size = wire_size(wire)
        if (
            size > RELAY_FORWARD_MAX_FRAME_BYTES
            or len(self._relay_pending) >= RELAY_FORWARD_MAX_PENDING
            or self._relay_pending_bytes + size > RELAY_FORWARD_MAX_PENDING_BYTES
        ):
            self._note_relay_dropped()
            return False
        self._relay_pending.append((item, wire, size, on_settled))
        self._relay_pending_bytes += size
        self._schedule_hint_flush()
        return True

    def next_hint_round(self) -> asyncio.Future[None] | None:
        """A future done once the next coalesced ``NODE_DRAIN_HINT`` frame —
        one taken after this call — went to every member peer (or the
        cluster stopped).

        What a forwarded item's settle waits for, without an item: a caller
        whose answer must take as long as a forward that found nobody (the
        public-highlight offer for a link that does not resolve, see
        ``routes/highlight_rtc.py``) waits on this. If nothing else is
        pending the frame goes out empty — still inside the frame pacing
        (:data:`DRAIN_HINT_MIN_GAP_S`), so waiters never add frames beyond
        it. ``None`` when cluster mode is off or
        :data:`FLUSH_WAITERS_MAX` callers already wait."""
        if not self._enabled or len(self._flush_waiters) >= FLUSH_WAITERS_MAX:
            return None
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._flush_waiters.append(waiter)
        self._schedule_hint_flush()
        return waiter

    def _release_flush_waiters(self, waiters: list[asyncio.Future[None]]) -> None:
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _note_relay_dropped(self) -> None:
        self._relay_dropped += 1
        now = self._clock()
        if now - self._relay_warned_at < RELAY_FORWARD_WARN_INTERVAL_S:
            return
        log.warning(
            "cluster: %d relay item(s) (%d bytes) already wait for the next "
            "drain hint — not forwarding %d more since the last warning; their "
            "subscribers on other nodes miss them live (offline copies are "
            "still queued, space sync catches up)",
            len(self._relay_pending),
            self._relay_pending_bytes,
            self._relay_dropped,
        )
        self._relay_dropped = 0
        self._relay_warned_at = now

    def attach_forwarded_relays(
        self, apply: Callable[[list[ForwardedRelay]], Awaitable[dict[str, list[str]]]]
    ) -> None:
        """Register ``ClusterRelayFanOut.apply`` — what an inbound
        ``NODE_DRAIN_HINT.relays`` runs."""
        self._relay_cb = apply

    async def apply_forwarded_relays(self, relays: object) -> dict[str, list[str]]:
        """Inbound ``NODE_DRAIN_HINT.relays`` — validate, push to the sockets
        held here (in the background) and return ``held`` for the reply.

        At most :data:`~.cluster_relay.RELAY_FORWARD_MAX_PER_FRAME` items are
        read; a malformed one is skipped. Never forwards an item again."""
        if self._relay_cb is None or not isinstance(relays, list):
            return {}
        items = [
            item
            for item in (
                ForwardedRelay.from_wire(raw)
                for raw in relays[:RELAY_FORWARD_MAX_PER_FRAME]
            )
            if item is not None
        ]
        if not items:
            return {}
        try:
            return await self._relay_cb(items)
        except Exception as exc:
            log.warning(
                "cluster: forwarded relay items not applied: %r", exc, exc_info=True
            )
            return {}

    def hint_subscriber_notice(
        self,
        owner: str,
        space_id: str,
        subscriber: str,
        *,
        reason: str,
        accepts: tuple[str, ...] = (),
    ) -> None:
        """Note a ``new_subscriber`` notice for an *owner* with no socket here.

        Synchronous and cheap — called from the subscribe / hello path. Rides
        the next coalesced ``NODE_DRAIN_HINT`` as ids only; the node holding
        the owner's socket rebuilds the notice from the shared DB. Repeats for
        one seat collapse, a NEW seat (``subscribe``) winning over a key
        repair. No-op when cluster mode is off (a single node has nobody to
        tell); past :data:`SUBSCRIBER_NOTICE_MAX_PENDING` the notice is
        dropped (the 5b-c reconcile and the next reconnect still repair it).
        """
        if not self._enabled:
            return
        key = (owner, space_id, subscriber)
        held = self._notice_pending.get(key)
        if held is None and len(self._notice_pending) >= SUBSCRIBER_NOTICE_MAX_PENDING:
            if not self._notice_dropped:
                log.warning(
                    "cluster: %d subscriber notices already wait for the next "
                    "drain hint — dropping more (the owners' reconcile repairs "
                    "them)",
                    len(self._notice_pending),
                )
            self._notice_dropped += 1
            return
        if held is None or reason == "subscribe":
            self._notice_pending[key] = (reason, tuple(accepts))
        self._schedule_hint_flush()

    def _schedule_hint_flush(self) -> None:
        if self._drain_hint_task is None or self._drain_hint_task.done():
            self._drain_hint_task = asyncio.get_running_loop().create_task(
                self._flush_drain_hints(), name="gfs-cluster-drain-hint"
            )

    def _take_pending_notices(self) -> list[dict]:
        notices = [
            {
                "owner": owner,
                "space_id": space_id,
                "subscriber": subscriber,
                "reason": reason,
                "accepts": list(accepts),
            }
            for (owner, space_id, subscriber), (
                reason,
                accepts,
            ) in self._notice_pending.items()
        ]
        self._notice_pending.clear()
        self._notice_dropped = 0
        return notices

    async def _hint_wait(self, *, min_delay: float) -> bool:
        """Sleep at least *min_delay*, and until :data:`DRAIN_HINT_MIN_GAP_S`
        has passed since the last frame. ``False`` once ``stop`` was called."""
        delay = max(
            min_delay, self._hint_sent_at + DRAIN_HINT_MIN_GAP_S - self._clock()
        )
        if delay > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        return not self._stop.is_set()

    def _take_relay_chunks(self) -> list[list[tuple]]:
        """Pending relay items, split into frames by count and bytes."""
        pending, self._relay_pending = self._relay_pending, []
        self._relay_pending_bytes = 0
        chunks: list[list[tuple]] = []
        size = 0
        for entry in pending:
            if (
                not chunks
                or len(chunks[-1]) >= RELAY_FORWARD_MAX_PER_FRAME
                or size + entry[2] > RELAY_FORWARD_MAX_FRAME_BYTES
            ):
                chunks.append([])
                size = 0
            chunks[-1].append(entry)
            size += entry[2]
        return chunks

    async def _flush_drain_hints(self) -> None:
        """Send pending ids, owner notices and relay items after the
        coalescing delay, paced to :data:`DRAIN_HINT_MIN_GAP_S` between
        frames; repeat while new ones arrived during a send. Ends on ``stop``
        (the ``_stop`` event), which settles relay items still unsent."""
        while (
            self._drain_hint_pending
            or self._notice_pending
            or self._relay_pending
            or self._flush_waiters
        ) and not self._stop.is_set():
            if not await self._hint_wait(min_delay=DRAIN_HINT_DELAY_S):
                return
            waiters, self._flush_waiters = self._flush_waiters, []
            batch = sorted(self._drain_hint_pending)
            self._drain_hint_pending.clear()
            notices = self._take_pending_notices()
            chunks = self._take_relay_chunks()
            frames = max(
                math.ceil(len(batch) / DRAIN_HINT_MAX_IDS),
                math.ceil(len(notices) / SUBSCRIBER_NOTICE_MAX_PER_FRAME),
                len(chunks),
                # Waiters alone still send one (empty) frame.
                1,
            )
            for index in range(frames):
                if index and not await self._hint_wait(min_delay=0.0):
                    # Stopping: ``stop`` settles the unsent relay items.
                    for chunk in chunks[index:]:
                        self._relay_pending.extend(chunk)
                    return
                ids = batch[index * DRAIN_HINT_MAX_IDS :][:DRAIN_HINT_MAX_IDS]
                notes = notices[index * SUBSCRIBER_NOTICE_MAX_PER_FRAME :][
                    :SUBSCRIBER_NOTICE_MAX_PER_FRAME
                ]
                chunk = chunks[index] if index < len(chunks) else []
                payload: dict = {"instances": ids}
                if notes:
                    payload["notices"] = notes
                if chunk:
                    payload["relays"] = [wire for _item, wire, _size, _cb in chunk]
                replies: list[dict | None] = []
                try:
                    replies = await self._broadcast_collect(NODE_DRAIN_HINT, payload)
                except asyncio.CancelledError:
                    # ``stop`` gave up waiting: it settles these after us.
                    for unsent in chunks[index:]:
                        self._relay_pending.extend(unsent)
                    if index == 0:
                        self._flush_waiters.extend(waiters)
                    raise
                except Exception as exc:
                    # Best effort: the household still gets the rows on its
                    # next hello, an owner its notice from its reconcile, an
                    # offline subscriber its copy from the origin's queue.
                    # Logged so a broken roster read is visible.
                    log.warning(
                        "cluster: drain hint for %d household(s), %d "
                        "subscriber notice(s) and %d relay item(s) not sent: %r",
                        len(ids),
                        len(notes),
                        len(chunk),
                        exc,
                        exc_info=True,
                    )
                self._hint_sent_at = self._clock()
                for item, _wire, _size, on_settled in chunk:
                    if on_settled is not None:
                        on_settled(held_from_replies(replies, item.audience))
                if index == 0:
                    self._release_flush_waiters(waiters)

    async def apply_drain_hint(self, instances: object) -> int:
        """Inbound ``NODE_DRAIN_HINT`` — drain the listed households whose
        socket is on THIS node.

        Each id must be a well-formed instance id
        (:data:`~socialhome.global_server.envelope_relay.ENVELOPE_INSTANCE_ID_RE`)
        — anything else is skipped, it is going into a log line and a SQL
        bind. Ids without a local socket are ignored: their node drains them,
        or their next hello does. Drains run as background tasks so the
        sender's ``/cluster/sync`` POST is answered at once. Returns how many
        drains were started.
        """
        if self._drain_cb is None or self._ws_registry is None:
            return 0
        if not isinstance(instances, list):
            return 0
        started = 0
        seen: set[str] = set()
        for raw in instances[:DRAIN_HINT_MAX_IDS]:
            if not isinstance(raw, str) or raw in seen:
                continue
            seen.add(raw)
            if not ENVELOPE_INSTANCE_ID_RE.fullmatch(raw):
                continue
            if not self._ws_registry.is_connected(raw):
                continue
            if raw in self._hint_drains:
                # A drain for it is already running and may have listed the
                # queue before the new row landed: run it once more after,
                # however many hints arrive meanwhile.
                self._hint_rerun.add(raw)
                continue
            task = asyncio.get_running_loop().create_task(
                self._run_hinted_drain(self._drain_cb, raw),
                name="gfs-cluster-hint-drain",
            )
            self._hint_drains[raw] = task
            task.add_done_callback(partial(self._forget_hint_drain, raw))
            started += 1
        return started

    def attach_subscriber_notice(self, deliver: NoticeDelivery) -> None:
        """Register ``GfsFederationService.deliver_forwarded_notice`` — what
        an inbound notice runs for an owner whose socket is on THIS node."""
        self._notice_cb = deliver

    async def apply_subscriber_notices(self, notices: object) -> int:
        """Inbound ``NODE_DRAIN_HINT.notices`` — deliver the ones whose owner
        has a socket on THIS node.

        Every field is validated before use: owner and subscriber must be
        well-formed instance ids, the space id a short printable string, the
        reason a known one; ``accepts`` is filtered like a subscribe's. At
        most :data:`SUBSCRIBER_NOTICE_MAX_PER_FRAME` are read. A notice for an
        owner without a socket here is ignored — its node got the same hint.
        Deliveries run as ONE background task so the sender's
        ``/cluster/sync`` POST is answered at once. Returns how many were
        scheduled."""
        if self._notice_cb is None or self._ws_registry is None:
            return 0
        if not isinstance(notices, list):
            return 0
        accepted: list[SubscriberNotice] = []
        for raw in notices[:SUBSCRIBER_NOTICE_MAX_PER_FRAME]:
            notice = _parse_notice(raw)
            if notice is None or not self._ws_registry.is_connected(notice.owner):
                continue
            accepted.append(notice)
        if not accepted:
            return 0
        task = asyncio.get_running_loop().create_task(
            self._deliver_notices(self._notice_cb, accepted),
            name="gfs-cluster-subscriber-notices",
        )
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)
        return len(accepted)

    async def _deliver_notices(
        self, deliver: NoticeDelivery, notices: list[SubscriberNotice]
    ) -> None:
        # Bounded (≤ SUBSCRIBER_NOTICE_MAX_PER_FRAME); ``stop`` awaits it like
        # a hinted drain, cancelling past its 5 s grace.
        for notice in notices:
            try:
                await deliver(
                    notice.owner,
                    notice.space_id,
                    notice.subscriber,
                    reason=notice.reason,
                    accepts=notice.accepts,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "cluster: subscriber notice delivery failed for space %s: %r",
                    notice.space_id,
                    exc,
                    exc_info=True,
                )

    def _forget_hint_drain(self, instance_id: str, task: asyncio.Task) -> None:
        if self._hint_drains.get(instance_id) is task:
            del self._hint_drains[instance_id]

    async def _run_hinted_drain(
        self, drain: Callable[[str], Awaitable[int]], instance_id: str
    ) -> None:
        while True:
            self._hint_rerun.discard(instance_id)
            try:
                await drain(instance_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "cluster: hint drain failed for %s: %r",
                    instance_id,
                    exc,
                    exc_info=True,
                )
            if instance_id not in self._hint_rerun or self._stop.is_set():
                return

    # ─── Admin-portal entry points ────────────────────────────────────

    async def add_peer(
        self,
        node_id: object,
        url: object,
        public_key: object,
    ) -> ClusterNode:
        """Operator approval of a peer node: approve its key, then HELLO it.

        The approved key is the node's cluster membership — its frames
        verify under it (:func:`authorize_frame`). A node already approved
        under a different key raises :class:`ClusterPeerKeyMismatch` (rotation is
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
        if (
            existing is not None
            and existing.approved_key
            and existing.approved_key.lower() != key
        ):
            raise ClusterPeerKeyMismatch(node_id)
        await self._repo.approve_node(node_id, url, key)
        # The read above and the write are not atomic: another approval may
        # have landed in between. The SQL keeps whichever key was stored
        # first (and its URL), so read back which one won.
        stored = next(
            (n for n in await self._repo.list_nodes() if n.node_id == node_id),
            None,
        )
        if stored is not None and stored.approved_key.lower() != key:
            raise ClusterPeerKeyMismatch(node_id)
        node = (
            replace(
                existing, url=url, public_key=key, approved_key=key, approved_url=url
            )
            if existing is not None
            else ClusterNode(
                node_id=node_id,
                url=url,
                public_key=key,
                approved_key=key,
                approved_url=url,
            )
        )
        try:
            await self._post_to_peer(
                url,
                NODE_HELLO,
                self._hello_payload(),
                to=node_id,
                session=None,
            )
        except Exception as exc:
            # Expected until the operator approves us on the other side too;
            # its own add-peer HELLO then reaches us and we answer it.
            log.debug(
                "cluster: initial NODE_HELLO to %r failed: %r", url, exc, exc_info=True
            )
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
        instance_id: object = None,
        instance_id_aliases: object = None,
    ) -> None:
        """Record a member's HELLO (online, ``last_seen`` now).

        Only called once :func:`authorize_frame` accepted the HELLO and its
        signature verified, so *public_key_hex* is the key it verified
        under — one this node already held. An existing row is refreshed
        (UPDATE only); a missing row is created only for a HELLO under our
        own key.
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
            # Our own HELLO (the peer list includes this node) — unless it
            # comes from another address: then two nodes share a node_id.
            self._note_own_node_id(url, public_key_hex)
            return
        self._note_sibling_identity(
            from_node_id, public_key_hex, instance_id, instance_id_aliases
        )
        # "Known" means we have heard from it before. An admin-added row has
        # never been seen (``last_seen`` is None) — answer its first HELLO so
        # the two sides converge whichever the operator added first.
        row = next(
            (n for n in await self._repo.list_nodes() if n.node_id == from_node_id),
            None,
        )
        now = _now_iso()
        if row is None:
            # Only a HELLO under our OWN key may create a row (a shared-seed
            # sibling's first contact). An approved peer's row is created by
            # the admin alone, so if it is gone the admin removed it while
            # this HELLO was in flight — and it stays removed.
            own = self._own_pk_hex.lower()
            if not own or public_key_hex.lower() != own:
                return
            url = normalized_peer_url(url)
            await self._repo.insert_node(
                ClusterNode(
                    node_id=from_node_id,
                    url=url,
                    public_key=own,
                    status="online",
                    last_seen=now,
                )
            )
            already_known = False
        elif (
            not row.approved_key
            and public_key_hex.lower() == self._own_pk_hex.lower()
            and not is_member(row, self._own_pk_hex)
        ):
            # A row with no approval that is not a member — an old-version
            # node sharing the DB rewrote it (``url`` + ``public_key``) for
            # some self-signed HELLO, or it predates this build. This HELLO
            # verified under our own key, so the sender holds the seed:
            # reclaim the row with our key and the URL the HELLO carries
            # (both proven together, so never a URL an outsider chose).
            # UPDATE only, and never on a row an admin approved meanwhile.
            url = normalized_peer_url(url)
            await self._repo.reclaim_node(
                from_node_id,
                url=url,
                public_key=self._own_pk_hex.lower(),
                status="online",
                last_seen=now,
            )
            already_known = False
        else:
            # An approved node's URL never moves in-band: its HELLO must not
            # point the row — and with it every later heartbeat and fan-out
            # POST — at another address; it is reached at the URL the
            # operator approved. A shared-seed sibling's URL follows its
            # HELLO (and its heartbeats): this HELLO verified under our OWN
            # seed, so the sender IS this GFS, and a shared-seed deployment
            # redeploys its allocs on changing ports — a frozen URL would aim
            # at a dead or wrong address. ``_sibling_url_refresh`` holds the
            # rule (no approval, a member under our own key, a usable base
            # URL that is not ours); the explicit key check here pins it to
            # the key this HELLO verified under. UPDATE only, and the repo
            # refuses to move a row an admin approved meanwhile.
            own = self._own_pk_hex.lower()
            refreshed = (
                self._sibling_url_refresh(row, url)
                if own and public_key_hex.lower() == own
                else ""
            )
            url = refreshed or member_url(row)
            await self._repo.touch_node(
                from_node_id, status="online", last_seen=now, sibling_url=refreshed
            )
            already_known = row.last_seen is not None
        if not already_known and url and self._enabled and self._node_id:
            # Reply only on FIRST contact so this can't ping-pong: the peer
            # already knows us by the time it processes this, so its own
            # handle_hello takes the ``already_known`` branch and stops.
            try:
                await self._post_to_peer(
                    url,
                    NODE_HELLO,
                    self._hello_payload(),
                    to=from_node_id,
                    session=None,
                )
            except Exception as exc:
                log.debug(
                    "cluster: reply NODE_HELLO to %r failed: %r",
                    url,
                    exc,
                    exc_info=True,
                )

    def _sibling_url_refresh(self, row: ClusterNode, url: object) -> str:
        """The URL to move a shared-seed sibling's *row* to, or ``""``.

        Only a sibling's URL moves in-band: the row has no ``approved_key``
        (an approved node is reached at the URL the operator approved) and
        is a member under our OWN key (:func:`is_member`) — so
        :func:`authorize_frame` verified the frame under our own key and the
        sender holds our seed. *url* must normalise to a usable base URL
        (:func:`normalized_peer_url`: never a metadata / link-local
        address), and must not be OUR url: a sibling announcing it means
        two nodes share one ``advertise_url``, and following it would make
        us heartbeat ourselves. That is refused with one WARNING per node.
        """
        own = self._own_pk_hex.lower()
        if row.approved_key or not own or not is_member(row, own):
            return ""
        refreshed = normalized_peer_url(url)
        if not refreshed:
            return ""
        if refreshed in (self._self_url, normalized_peer_url(self._self_url)):
            if row.node_id not in self._dup_url_warned:
                self._dup_url_warned.add(row.node_id)
                log.warning(
                    "cluster: sibling %s announces our own URL %s — two nodes "
                    "share one [cluster] advertise_url; give each node its "
                    "own address. Keeping its old URL.",
                    ascii(row.node_id),
                    ascii(refreshed),
                )
            return ""
        if refreshed != row.url:
            log.info(
                "cluster: sibling %s moved its URL %s -> %s",
                ascii(row.node_id),
                ascii(row.url),
                ascii(refreshed),
            )
        return refreshed

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
        ``payload['url']`` is the sender's advertised cluster URL; a
        shared-seed sibling's row follows it (:meth:`_sibling_url_refresh`).
        Older peers omit it and the row keeps its URL.
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
        announced = payload.get("url") if isinstance(payload, dict) else None
        rows = await self._repo.list_nodes()
        for r in rows:
            if r.node_id == from_node_id:
                if is_member(r, self._own_pk_hex):
                    # The key this heartbeat verified under
                    # (:func:`authorize_frame`): its approved key, else ours.
                    body = payload if isinstance(payload, dict) else {}
                    self._note_sibling_identity(
                        from_node_id,
                        r.approved_key or self._own_pk_hex,
                        body.get("instance_id"),
                        body.get("instance_id_aliases"),
                    )
                # UPDATE only: an admin removal since the read stays removed.
                # A shared-seed sibling's URL follows its heartbeat as it does
                # its HELLO; a heartbeat always names its recipient, so a
                # replay to another node is refused and cannot roll it back.
                await self._repo.touch_node(
                    r.node_id,
                    status="online",
                    last_seen=_now_iso(),
                    sibling_url=self._sibling_url_refresh(r, announced),
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
                    to=from_node_id,
                    session=session,
                )
            except Exception as exc:
                log.debug(
                    "cluster: NODE_PARTITION_GAP to %r failed: %r",
                    peer_url,
                    exc,
                    exc_info=True,
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
        to: str = "",
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
                to=to,
                session=session,
            )
        except Exception as exc:
            log.debug(
                "cluster: NODE_PARTITION_CATCHUP to %r failed: %r",
                peer_url,
                exc,
                exc_info=True,
            )

    async def _lookup_node_url(self, node_id: str) -> str:
        if not node_id:
            return ""
        for n in await self.member_peers():
            if n.node_id == node_id:
                return member_url(n)
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
            await self._announce_to_peers()
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=SYNC_RETRY_DELAY_S,
                )
                return
            except TimeoutError:
                pass

    def _hello_payload(self) -> dict:
        return {
            "node_id": self._node_id,
            "url": self._self_url,
            "public_key": self._own_pk_hex,
            # Our public id and transitional aliases, for the sibling
            # identity guard. Additive: older nodes ignore them.
            "instance_id": self._instance_id,
            "instance_id_aliases": sorted(self._instance_id_aliases),
        }

    # ─── Public-identity guard ───────────────────────────────────────

    def _note_sibling_identity(
        self,
        node_id: str,
        verified_key: str,
        reported: object,
        reported_aliases: object = None,
    ) -> None:
        """Compare a sibling's reported ``instance_id`` with ours.

        Only a node whose frame verified under our OWN identity key is
        compared: it IS this GFS to every household (same key, same
        ``base_url`` behind the load balancer), so it must answer under the
        same public id — otherwise a household pins whichever id the node it
        reached served, and requests the balancer sends to another node are
        refused as addressed elsewhere. A peer under its own (approved) key
        is another server and may have another id.

        A different id linked through either side's ``instance_id_aliases``
        is a rolling id change in progress (WARNING, admin flag); any other
        difference is a misconfiguration (ERROR, admin flag). Neither fails
        ``/healthz`` — pulling every node out of the balancer is worse than
        the misconfiguration. A missing / empty field is UNKNOWN, never a
        mismatch: an older node does not send it.
        """
        own = self._own_pk_hex.lower()
        if not own or not self._instance_id or verified_key.lower() != own:
            return
        if not isinstance(reported, str) or not reported:
            return
        theirs = (
            {a for a in reported_aliases if isinstance(a, str)}
            if isinstance(reported_aliases, list)
            else set()
        )
        if reported == self._instance_id:
            cleared = self._identity_mismatches.pop(node_id, None)
            cleared = self._identity_transitional.pop(node_id, None) or cleared
            if cleared is not None:
                log.info(
                    "cluster: sibling %s now serves our instance_id %s",
                    ascii(node_id),
                    ascii(self._instance_id),
                )
            return
        now = self._clock()
        if reported in self._instance_id_aliases or self._instance_id in theirs:
            self._identity_mismatches.pop(node_id, None)
            previous = self._identity_transitional.get(node_id)
            self._identity_transitional[node_id] = (reported, now)
            if previous is None or previous[0] != reported:
                log.warning(
                    "cluster: sibling %s serves instance_id %s, we serve %s — "
                    "linked through instance_id_aliases, so a rolling change of "
                    "the public id is in progress. Finish rolling every node "
                    "onto the same [server] instance_id.",
                    ascii(node_id),
                    ascii(reported),
                    ascii(self._instance_id),
                )
            return
        self._identity_transitional.pop(node_id, None)
        previous = self._identity_mismatches.get(node_id)
        self._identity_mismatches[node_id] = (reported, now)
        if previous is None or previous[0] != reported:
            log.error(
                "cluster: sibling %s shares our identity key but serves "
                "instance_id %s, we serve %s. Households pin the id they were "
                "served and sign it into every request, so requests routed to "
                "the other node are refused. Set the SAME [server] instance_id "
                "on every node (list the old ids in instance_id_aliases while "
                "households migrate).",
                ascii(node_id),
                ascii(reported),
                ascii(self._instance_id),
            )

    def _note_own_node_id(self, url: object, verified_key: str) -> None:
        """A HELLO under our key naming OUR node_id: our own announce (the
        peer list includes this node) — or, from another address, a second
        node with the same ``node_id`` (ERROR + admin flag)."""
        own = self._own_pk_hex.lower()
        if not own or verified_key.lower() != own:
            return
        theirs = normalized_peer_url(url)
        if not theirs or theirs in (
            self._self_url,
            normalized_peer_url(self._self_url),
        ):
            return
        first = theirs not in self._duplicate_node_urls
        self._duplicate_node_urls[theirs] = self._clock()
        if first:
            log.error(
                "cluster: the node at %s announces OUR node_id %s — two nodes "
                "share one node_id and cannot be told apart. Set a unique "
                "[cluster] node_id on every node (an empty one falls back to "
                "[server] instance_id, which every node shares).",
                ascii(theirs),
                ascii(self._node_id),
            )

    def _live(self, store: dict) -> dict:
        now = self._clock()
        for key, value in list(store.items()):
            at = value[1] if isinstance(value, tuple) else value
            if now - at > CLUSTER_IDENTITY_MISMATCH_TTL_S:
                del store[key]
        return store

    def identity_mismatches(self) -> list[dict]:
        """Siblings under our key that recently reported another, unlinked
        ``instance_id``: ``[{node_id, instance_id}]``, sorted by node id."""
        return [
            {"node_id": node_id, "instance_id": reported}
            for node_id, (reported, _at) in sorted(
                self._live(self._identity_mismatches).items()
            )
        ]

    def identity_transitional(self) -> list[dict]:
        """Siblings whose differing ``instance_id`` is linked to ours through
        ``instance_id_aliases`` — a rolling id change in progress."""
        return [
            {"node_id": node_id, "instance_id": reported}
            for node_id, (reported, _at) in sorted(
                self._live(self._identity_transitional).items()
            )
        ]

    def duplicate_node_id_urls(self) -> list[str]:
        """Addresses that recently announced OUR ``node_id``."""
        return sorted(self._live(self._duplicate_node_urls))

    async def _announce_to_peers(self) -> None:
        """HELLO every configured peer URL (fail-soft per peer).

        A URL we already hold a row for names that node as the recipient
        (``to``) — config peers are normalised at load exactly like stored
        URLs, so the two match; a URL we have never heard from is HELLOed
        without one —
        the configured peer list (Nomad renders it) carries no node ids,
        and it may include this node itself, whose own HELLO is ignored.
        """
        known = {member_url(n): n.node_id for n in await self.member_peers()}
        self_url = normalized_peer_url(self._self_url)
        for peer_url in self._peers:
            if not peer_url or peer_url in (self._self_url, self_url):
                continue
            try:
                await self._post_to_peer(
                    peer_url,
                    NODE_HELLO,
                    self._hello_payload(),
                    to=known.get(peer_url, ""),
                    session=None,
                )
            except Exception as exc:
                log.debug(
                    "cluster: NODE_HELLO to %r failed: %r", peer_url, exc, exc_info=True
                )

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=HEARTBEAT_INTERVAL_S,
                    )
                    return
                except TimeoutError:
                    pass
                await self._heartbeat_tick()
        except asyncio.CancelledError:
            return

    async def _heartbeat_tick(self) -> None:
        """Ping every known peer once and record its liveness.

        Liveness writes are UPDATE-only (``touch_node``): a ping can take
        seconds, and an admin may remove — or remove and re-add under a new
        key — the peer meanwhile. Re-writing the row read before the ping
        would undo that.

        First drops shared-seed sibling rows unseen for
        :data:`CLUSTER_STALE_SIBLING_S` (never an approved row).
        """
        await self._collect_stale_siblings()
        for r in await self.member_peers():
            url = member_url(r)
            if r.status == "offline":
                # Probe offline peers too — coming back triggers
                # a partition-catchup handshake (spec §4.4.6).
                if await self._ping_peer(url):
                    self._fail_counts[url] = 0
                    await self._repo.touch_node(
                        r.node_id, status="online", last_seen=_now_iso()
                    )
                    await self.announce_partition_catchup(url, to=r.node_id)
                continue
            ok = await self._ping_peer(url)
            fails = self._fail_counts.get(url, 0)
            if ok:
                self._fail_counts[url] = 0
                await self._repo.touch_node(
                    r.node_id, status="online", last_seen=_now_iso()
                )
                # Spec §24.10.7 — propagate own sync-signaling load
                # via NODE_HEARTBEAT so peers' selectors see fresh
                # counts on the next ``pick_signaling_node``.
                try:
                    await self._post_to_peer(
                        url,
                        NODE_HEARTBEAT,
                        {
                            "active_sync_sessions": self._active_sync_count.get(
                                self._node_id,
                                0,
                            ),
                            "connected_clients": self._own_connected_clients(),
                            # Our advertised cluster URL: a shared-seed
                            # sibling's row for us follows it. Older peers
                            # ignore the key.
                            "url": self._self_url,
                            # Our public id + aliases, for the sibling
                            # identity guard.
                            "instance_id": self._instance_id,
                            "instance_id_aliases": sorted(self._instance_id_aliases),
                        },
                        to=r.node_id,
                        session=None,
                    )
                except Exception as exc:
                    log.debug(
                        "cluster: NODE_HEARTBEAT to %r failed: %r",
                        url,
                        exc,
                        exc_info=True,
                    )
            else:
                self._fail_counts[url] = fails + 1
                if fails + 1 >= HEARTBEAT_FAIL_THRESHOLD:
                    await self._repo.touch_node(
                        r.node_id, status="offline", last_seen=r.last_seen
                    )

    async def _collect_stale_siblings(self) -> None:
        """Delete shared-seed sibling rows (no approval, our own key) whose
        last sign of life is older than :data:`CLUSTER_STALE_SIBLING_S`.

        A removed sibling that is still alive rejoins with its next HELLO —
        its re-announce loop sends one every few seconds. Rows an operator
        approved are never touched, nor rows that are no member (the admin
        view shows them so a distinct-key peer can be re-added).
        """
        if not self._own_pk_hex:
            return
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=CLUSTER_STALE_SIBLING_S)
        ).strftime("%Y-%m-%d %H:%M:%S")
        try:
            removed = await self._repo.remove_stale_siblings(
                own_key=self._own_pk_hex.lower(),
                seen_before=cutoff,
                keep_node_id=self._node_id,
            )
        except Exception as exc:
            log.warning("cluster: stale sibling cleanup failed: %r", exc, exc_info=True)
            return
        if removed:
            log.info("cluster: dropped %d stale shared-seed sibling row(s)", removed)

    async def _broadcast(
        self,
        msg_type: str,
        payload: dict,
        *,
        ignore_errors: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        for r in await self.member_peers():
            if r.status == "offline":
                continue
            url = member_url(r)
            try:
                await self._post_to_peer(
                    url, msg_type, payload, to=r.node_id, session=session
                )
                self._fail_counts[url] = 0
            except Exception as exc:
                if ignore_errors:
                    log.debug(
                        "cluster: %s to %r failed: %r",
                        msg_type,
                        url,
                        exc,
                        exc_info=True,
                    )
                    continue
                await asyncio.sleep(SYNC_RETRY_DELAY_S)
                try:
                    await self._post_to_peer(
                        url,
                        msg_type,
                        payload,
                        to=r.node_id,
                        session=session,
                    )
                except Exception as exc2:
                    log.warning(
                        "cluster: %s to %r dropped after retry: %r",
                        msg_type,
                        url,
                        exc2,
                        exc_info=True,
                    )

    async def _broadcast_collect(
        self, msg_type: str, payload: dict
    ) -> list[dict | None]:
        """Send one frame to every online member peer CONCURRENTLY and return
        each peer's JSON reply (``None`` for a failed send or a reply that
        is not a JSON object). Never retries — the callers are best effort."""
        peers = [r for r in await self.member_peers() if r.status != "offline"]

        async def _one(row: ClusterNode) -> dict | None:
            url = member_url(row)
            try:
                reply = await self._post_to_peer(
                    url, msg_type, payload, to=row.node_id, read_reply=True
                )
            except Exception as exc:
                log.debug(
                    "cluster: %s to %r failed: %r", msg_type, url, exc, exc_info=True
                )
                return None
            self._fail_counts[url] = 0
            return reply

        return list(await asyncio.gather(*(_one(r) for r in peers)))

    def _signed_frame(
        self, msg_type: str, payload: dict, *, to: str = ""
    ) -> tuple[bytes, str]:
        """Build a ``/cluster/sync`` frame: ``(canonical bytes, signature)``.

        *to* is the recipient's node id, signed into the body so the frame
        is refused by any other node (409 ``wrong_recipient``). Left out
        when the caller only knows a URL (a HELLO to a configured peer).
        """
        body: dict = {
            "type": msg_type,
            "from": self._node_id,
            "ts": int(self._wall_clock()),
            "nonce": b64url_encode(secrets.token_bytes(CLUSTER_NONCE_BYTES)),
            "sig_suite": CLUSTER_SIG_SUITE_ED25519,
            "payload": payload,
        }
        if to:
            body["to"] = to
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
        return canonical, sig

    async def _post_to_peer(
        self,
        peer_url: str,
        msg_type: str,
        payload: dict,
        *,
        to: str = "",
        session: aiohttp.ClientSession | None = None,
        read_reply: bool = False,
    ) -> dict | None:
        """POST one signed frame; raise on a transport error or a status
        >= 300. With *read_reply* return the reply's JSON object (``None``
        when it is not one, or larger than
        :data:`DRAIN_HINT_REPLY_MAX_BYTES`)."""
        canonical, sig = self._signed_frame(msg_type, payload, to=to)
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
                if not read_reply:
                    return None
                raw = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    raw += chunk
                    if len(raw) > DRAIN_HINT_REPLY_MAX_BYTES:
                        return None
                try:
                    reply = json.loads(raw)
                except ValueError:
                    return None
                return reply if isinstance(reply, dict) else None
        finally:
            if own_session:
                await active.close()

    async def _ping_peer(self, peer_url: str) -> bool:
        try:
            async with (
                aiohttp.ClientSession() as sess,
                sess.get(
                    f"{peer_url.rstrip('/')}/cluster/health",
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp,
            ):
                return 200 <= resp.status < 300
        except Exception:
            log.debug(
                "ClusterService._ping_peer: fail-soft error ignored", exc_info=True
            )
            return False

    def _gc_seen(self) -> None:
        cutoff = time.monotonic() - 600.0
        stale = [k for k, v in self._seen_relays.items() if v < cutoff]
        for k in stale:
            self._seen_relays.pop(k, None)


# ─── Wire shape helpers ──────────────────────────────────────────────────


def _key_view(row: ClusterNode, own_key_lower: str) -> dict:
    """``{public_key, key_source}`` for a peer row in the admin view.

    ``key_source`` says which key the row's frames verify under:

    * ``approved`` — the key an admin approved (``approved_key``);
    * ``own`` — our own identity key: the admin approved our key for it,
      or it has no approval and its last HELLO verified under our key (a
      shared-seed sibling);
    * ``none`` — no approval and no sign of the shared seed: an older row
      (TOFU or a distinct-key peer from before approvals). Its frames
      verify only under our own key, so a peer with its own key must be
      re-added.

    ``public_key`` is the key shown: the approved key, ours for ``own``, or
    ``""``. The legacy ``public_key`` column only tells ``own`` from
    ``none`` for display — it can only hold our key if a HELLO verified
    under it (or the operator approved it), and it never grants anything.
    """
    approved = row.approved_key.lower()
    if approved:
        if own_key_lower and approved == own_key_lower:
            return {"public_key": approved, "key_source": "own"}
        return {"public_key": approved, "key_source": "approved"}
    if own_key_lower and row.public_key.lower() == own_key_lower:
        return {"public_key": own_key_lower, "key_source": "own"}
    return {"public_key": "", "key_source": "none"}


def _parse_notice(raw: object) -> SubscriberNotice | None:
    """A forwarded owner notice off the wire, or ``None`` if malformed."""
    if not isinstance(raw, dict):
        return None
    owner, space_id = raw.get("owner"), raw.get("space_id")
    subscriber, reason = raw.get("subscriber"), raw.get("reason")
    if not (
        isinstance(owner, str)
        and ENVELOPE_INSTANCE_ID_RE.fullmatch(owner)
        and isinstance(subscriber, str)
        and ENVELOPE_INSTANCE_ID_RE.fullmatch(subscriber)
        and isinstance(space_id, str)
        and 0 < len(space_id) <= SUBSCRIBER_NOTICE_SPACE_ID_MAX_LEN
        and space_id.isprintable()
        and isinstance(reason, str)
        and reason in _NOTICE_REASONS
    ):
        return None
    return SubscriberNotice(
        owner=owner,
        space_id=space_id,
        subscriber=subscriber,
        reason=reason,
        accepts=parse_accepts(raw.get("accepts")),
    )


def _node_address_key(node_id: str, client_ip: str) -> str:
    """Unambiguous limiter key for a (node id, address) pair — a node id
    may contain any character, so no separator would do."""
    return json.dumps([node_id, client_ip])


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
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _client_to_wire(c: ClientInstance) -> dict:
    return {
        "instance_id": c.instance_id,
        "display_name": c.display_name,
        "public_key": c.public_key,
        "status": c.status,
        "auto_accept": c.auto_accept,
        "connected_at": c.connected_at,
    }


def _wire_to_client(d: dict) -> ClientInstance:
    """Only the fields the row has; an ``inbox_url`` sent by a node still on
    the release before migration 0018 is ignored."""
    return ClientInstance(
        instance_id=str(d["instance_id"]),
        display_name=str(d.get("display_name") or ""),
        public_key=str(d.get("public_key") or ""),
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
        # The owner's place pin travels too, or a peer sync would drop it
        # until the owner's next publish.
        "place": _place_wire(s),
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
        # Re-validated like any wire point; malformed ⇒ no pin.
        place_lat=_wire_place(d)[0],
        place_lon=_wire_place(d)[1],
        place_radius_km=_wire_place(d)[2],
    )


def _place_wire(s: GlobalSpace) -> dict | None:
    """A listing's pin (and radius hint) as gossiped, or ``None``."""
    if s.place_lat is None or s.place_lon is None:
        return None
    return place_pin_wire(
        PlacePin(lat=s.place_lat, lon=s.place_lon, radius_km=s.place_radius_km)
    )


def _wire_place(d: dict) -> tuple[float | None, float | None, float | None]:
    """A gossiped place pin, re-validated; malformed or absent ⇒ none (a
    malformed hint alone ⇒ the pin without it)."""
    pin = parse_place_pin_wire(d.get("place"))
    if pin is None:
        return (None, None, None)
    return (pin.lat, pin.lon, pin.radius_km)


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

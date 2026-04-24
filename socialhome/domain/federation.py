"""Federation domain types (§4.1 / §11 / §24).

Only pure dataclasses and enums live here — no I/O, no service logic.

The :class:`FederationEventType` enum is the wire vocabulary. Adding a new
event type is a protocol change and must be done with care.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


# ─── Event type vocabulary (§24.11) ───────────────────────────────────────


class FederationEventType(str, enum.Enum):
    """All federation event types exchanged between instances.

    Values are the literal strings used on the wire. The ``str`` mixin means
    ``FederationEventType.FOO == "foo"`` evaluates true, which simplifies
    JSON parsing.
    """

    # ── Pairing ──
    PAIRING_INTRO = "pairing_intro"
    PAIRING_INTRO_RELAY = "pairing_intro_relay"
    PAIRING_INTRO_AUTO = "pairing_intro_auto"
    PAIRING_INTRO_AUTO_ACK = "pairing_intro_auto_ack"
    PAIRING_ACCEPT = "pairing_accept"
    PAIRING_CONFIRM = "pairing_confirm"
    PAIRING_ABORT = "pairing_abort"
    UNPAIR = "unpair"
    URL_UPDATED = "url_updated"

    # ── User sync ──
    USERS_SYNC = "users_sync"
    USER_UPDATED = "user_updated"
    USER_REMOVED = "user_removed"
    USER_STATUS_UPDATED = "user_status_updated"

    # ── Space structural ──
    SPACE_CREATED = "space_created"
    SPACE_AGE_GATE_UPDATED = "space_age_gate_updated"
    SPACE_CONFIG_CHANGED = "space_config_changed"
    SPACE_CONFIG_CATCH_UP = "space_config_catch_up"
    SPACE_DISSOLVED = "space_dissolved"
    SPACE_INSTANCE_LEFT = "space_instance_left"
    SPACE_MEMBER_JOINED = "space_member_joined"
    SPACE_MEMBER_LEFT = "space_member_left"
    SPACE_MEMBER_BANNED = "space_member_banned"
    SPACE_MEMBER_UNBANNED = "space_member_unbanned"

    # ── Space invitations / join requests ──
    SPACE_INVITE = "space_invite"
    SPACE_INVITE_VIA = "space_invite_via"
    SPACE_ACCEPT = "space_accept"
    SPACE_JOIN_REQUEST = "space_join_request"
    SPACE_JOIN_REQUEST_VIA = "space_join_request_via"
    SPACE_JOIN_REQUEST_REPLY_VIA = "space_join_request_reply_via"
    SPACE_JOIN_REQUEST_APPROVED = "space_join_request_approved"
    SPACE_JOIN_REQUEST_DENIED = "space_join_request_denied"
    SPACE_JOIN_REQUEST_EXPIRED = "space_join_request_expired"
    SPACE_JOIN_REQUEST_WITHDRAWN = "space_join_request_withdrawn"

    # ── Space content ──
    SPACE_POST_CREATED = "space_post_created"
    SPACE_POST_UPDATED = "space_post_updated"
    SPACE_POST_DELETED = "space_post_deleted"
    SPACE_COMMENT_CREATED = "space_comment_created"
    SPACE_COMMENT_UPDATED = "space_comment_updated"
    SPACE_COMMENT_DELETED = "space_comment_deleted"
    SPACE_MEMBER_PROFILE_UPDATED = "space_member_profile_updated"
    SPACE_PAGE_CREATED = "space_page_created"
    SPACE_PAGE_UPDATED = "space_page_updated"
    SPACE_PAGE_DELETED = "space_page_deleted"
    SPACE_TASK_CREATED = "space_task_created"
    SPACE_TASK_UPDATED = "space_task_updated"
    SPACE_TASK_DELETED = "space_task_deleted"
    SPACE_POLL_CREATED = "space_poll_created"
    SPACE_POLL_VOTE_CAST = "space_poll_vote_cast"
    SPACE_POLL_CLOSED = "space_poll_closed"
    SPACE_STICKY_CREATED = "space_sticky_created"
    SPACE_STICKY_UPDATED = "space_sticky_updated"
    SPACE_STICKY_DELETED = "space_sticky_deleted"
    SPACE_CALENDAR_EVENT_CREATED = "space_calendar_event_created"
    SPACE_CALENDAR_EVENT_UPDATED = "space_calendar_event_updated"
    SPACE_CALENDAR_EVENT_DELETED = "space_calendar_event_deleted"
    SPACE_SCHEDULE_RESPONSE_UPDATED = "space_schedule_response_updated"
    SPACE_SCHEDULE_FINALIZED = "space_schedule_finalized"
    SPACE_LOCATION_UPDATED = "space_location_updated"

    # ── Space encryption key exchange ──
    SPACE_KEY_EXCHANGE = "space_key_exchange"
    SPACE_KEY_EXCHANGE_ACK = "space_key_exchange_ack"
    SPACE_KEY_EXCHANGE_REKEY = "space_key_exchange_rekey"
    SPACE_ADMIN_KEY_SHARE = "space_admin_key_share"
    SPACE_SESSION_CLEANUP = "space_session_cleanup"

    # ── Space sync ──
    SPACE_SYNC_BEGIN = "space_sync_begin"
    SPACE_SYNC_CHUNK = "space_sync_chunk"
    SPACE_SYNC_CHUNK_ACK = "space_sync_chunk_ack"
    SPACE_SYNC_RESUME = "space_sync_resume"
    SPACE_SYNC_COMPLETE = "space_sync_complete"
    SPACE_SYNC_OFFER = "space_sync_offer"
    SPACE_SYNC_ANSWER = "space_sync_answer"
    SPACE_SYNC_ICE = "space_sync_ice"
    SPACE_SYNC_DIRECT_READY = "space_sync_direct_ready"
    SPACE_SYNC_DIRECT_FAILED = "space_sync_direct_failed"
    SPACE_SYNC_REQUEST_MORE = "space_sync_request_more"

    # ── Resilience / partition handling ──
    INSTANCE_SYNC_STATUS = "instance_sync_status"
    SPACE_PARTITION_GAP = "space_partition_gap"
    NODE_PARTITION_CATCHUP = "node_partition_catchup"
    NODE_PARTITION_GAP = "node_partition_gap"

    # ── DM relay ──
    DM_USER_TYPING = "dm_user_typing"
    DM_RELAY = "dm_relay"
    DM_MESSAGE = "dm_message"
    DM_MESSAGE_DELETED = "dm_message_deleted"
    DM_MESSAGE_REACTION = "dm_message_reaction"
    DM_MEMBER_ADDED = "dm_member_added"
    DM_CONTACT_REQUEST = "dm_contact_request"
    DM_CONTACT_ACCEPTED = "dm_contact_accepted"
    DM_CONTACT_DECLINED = "dm_contact_declined"
    DM_HISTORY_REQUEST = "dm_history_request"
    DM_HISTORY_CHUNK = "dm_history_chunk"
    DM_HISTORY_CHUNK_ACK = "dm_history_chunk_ack"
    DM_HISTORY_COMPLETE = "dm_history_complete"

    # ── Public space advertisement ──
    PUBLIC_SPACE_ADVERTISE = "public_space_advertise"
    PUBLIC_SPACE_WITHDRAWN = "public_space_withdrawn"

    # ── Peer-to-peer public-space directory sync (§D1a) ──
    # One household publishes a snapshot of its ``type=public`` spaces
    # to each CONFIRMED peer so the peer's space browser can list them
    # under "From friends" without going via the GFS.
    SPACE_DIRECTORY_SYNC = "space_directory_sync"

    # ── Cross-household invites for private spaces (§D1b, zero-leak) ──
    # Plaintext envelope carries only routing fields. All space metadata
    # (space_id, display hint, inviter, invite_token) rides inside the
    # encrypted payload. See `federation/private_invite_handler.py`.
    SPACE_PRIVATE_INVITE = "space_private_invite"
    SPACE_PRIVATE_INVITE_ACCEPT = "space_private_invite_accept"
    SPACE_PRIVATE_INVITE_DECLINE = "space_private_invite_decline"
    SPACE_REMOTE_MEMBER_REMOVED = "space_remote_member_removed"

    # ── Moderation ──
    SPACE_REPORT = "space_report"

    # ── Presence ──
    PRESENCE_UPDATED = "presence_updated"

    # ── WebRTC / calls ──
    CALL_OFFER = "call_offer"
    CALL_ANSWER = "call_answer"
    CALL_DECLINE = "call_decline"
    CALL_BUSY = "call_busy"
    CALL_HANGUP = "call_hangup"
    CALL_END = "call_end"
    CALL_ICE = "call_ice"
    CALL_ICE_CANDIDATE = "call_ice_candidate"
    CALL_QUALITY = "call_quality"

    # ── Network discovery ──
    NETWORK_SYNC = "network_sync"

    # ── P2P federation-level WebRTC signalling (§24.12.5) ──
    # Used to bootstrap a persistent DataChannel between paired
    # Social Home instances over the existing signed HTTPS inbox. Once
    # the channel is open, routine federation envelopes are delivered
    # over it and HTTPS acts as fallback only.
    FEDERATION_RTC_OFFER = "federation_rtc_offer"
    FEDERATION_RTC_ANSWER = "federation_rtc_answer"
    FEDERATION_RTC_ICE = "federation_rtc_ice"


# Subsets used throughout the service layer ────────────────────────────────

#: Events whose plaintext payloads are permitted to carry routing metadata
#: only. The encrypted envelope is required for anything else (§25.8.20–21).
PAIRING_EVENTS: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.PAIRING_INTRO,
        FederationEventType.PAIRING_INTRO_RELAY,
        FederationEventType.PAIRING_INTRO_AUTO,
        FederationEventType.PAIRING_INTRO_AUTO_ACK,
        FederationEventType.PAIRING_ACCEPT,
        FederationEventType.PAIRING_CONFIRM,
        FederationEventType.PAIRING_ABORT,
        FederationEventType.UNPAIR,
    }
)

#: Structural events that must survive retention pruning (§4.4.7 / §25.8.19).
STRUCTURAL_EVENTS: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.SPACE_CREATED,
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.SPACE_CONFIG_CHANGED,
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_MEMBER_LEFT,
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        FederationEventType.SPACE_ADMIN_KEY_SHARE,
    }
)


# ─── Pairing state machine (§11) ──────────────────────────────────────────


class PairingStatus(str, enum.Enum):
    PENDING_SENT = "pending_sent"
    PENDING_RECEIVED = "pending_received"
    CONFIRMED = "confirmed"
    UNPAIRING = "unpairing"


class InstanceSource(str, enum.Enum):
    """How a remote_instances row came into existence.

    ``manual`` — classic QR-based pairing (§11).
    ``space_session`` — derived at space-join time from the admin key share
    flow, without ever exchanging a full pairing handshake (§13).
    """

    MANUAL = "manual"
    SPACE_SESSION = "space_session"


# ─── RemoteInstance (§4.1 / remote_instances table) ───────────────────────


@dataclass(slots=True, frozen=True)
class RemoteInstance:
    """A peer instance we know about.

    Mirrors the ``remote_instances`` row. All keys are stored KEK-encrypted at
    rest on the DB layer; the :class:`KeyManager` decrypts before handing the
    value to this dataclass.
    """

    id: str  # 32-char instance_id (derive_instance_id)
    display_name: str
    remote_identity_pk: str  # 64 hex chars — Ed25519 public key
    key_self_to_remote: str  # AES-256-GCM session key (ciphertext)
    key_remote_to_self: str  # AES-256-GCM session key (ciphertext)
    remote_inbox_url: str
    local_inbox_id: str
    status: PairingStatus = PairingStatus.CONFIRMED
    intro_relay_enabled: bool = True
    source: InstanceSource = InstanceSource.MANUAL
    proto_version: int = 1
    # Post-quantum identity material advertised by the peer during
    # pairing. ``remote_pq_algorithm`` is non-None when the peer supports
    # a PQ suite; ``remote_pq_identity_pk`` is the hex-encoded PQ public
    # key. Both stay ``None`` on classical peers.
    remote_pq_algorithm: str | None = None
    remote_pq_identity_pk: str | None = None
    #: Per-peer negotiated wire suite — see ``federation.crypto_suite``.
    #: Default ``"ed25519"`` matches the classical (and safest-floor)
    #: behaviour when the remote side doesn't advertise a hybrid key.
    sig_suite: str = "ed25519"
    relay_via: str | None = None  # introducer instance_id, if introduced
    home_lat: float | None = None  # 4dp-truncated
    home_lon: float | None = None
    paired_at: str | None = None
    created_at: str | None = None
    last_reachable_at: str | None = None
    unreachable_since: str | None = None

    def is_reachable(self) -> bool:
        return self.unreachable_since is None


# ─── Wire envelope (§24.11) ───────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class FederationEnvelope:
    """Cleartext routing envelope of a federation message.

    Only routing metadata is in the clear. The ``encrypted_payload`` field
    carries the AES-256-GCM ciphertext produced from the directional
    session key (§25.8.20–21).
    """

    msg_id: str  # UUID; used by ReplayCache
    event_type: FederationEventType
    from_instance: str  # instance_id
    to_instance: str  # instance_id
    timestamp: str  # ISO-8601 UTC
    encrypted_payload: str  # b64url(nonce:ciphertext:tag)
    signature: str  # b64url Ed25519 signature
    space_id: str | None = None  # space-scoped events only
    epoch: int | None = None  # encryption epoch (§25.8.20)
    proto_version: int = 1


@dataclass(slots=True, frozen=True)
class DecryptedPayload:
    """The decrypted inner JSON of a federation message.

    Contains the ``event_type`` repeated (for matching) and the full
    event-specific payload dict.
    """

    event_type: FederationEventType
    payload: dict


# ─── Pairing in-flight state ──────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PairingSession:
    """An in-progress pairing handshake (§11)."""

    token: str  # URL-safe random token from QR
    own_identity_pk: str  # 64 hex
    own_dh_pk: str  # 64 hex (X25519)
    own_dh_sk: str  # 64 hex (X25519) — kept until confirm
    inbox_url: str
    peer_identity_pk: str | None = None
    peer_dh_pk: str | None = None
    peer_inbox_url: str | None = None
    intro_note: str | None = None
    relay_via: str | None = None
    verification_code: str | None = None  # 6-digit SAS
    issued_at: str | None = None
    expires_at: str | None = None
    status: PairingStatus = PairingStatus.PENDING_SENT


# ─── Broadcast / delivery results ─────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class DeliveryResult:
    """Result of sending a single federation message to a single peer."""

    instance_id: str
    ok: bool
    status_code: int | None = None
    error: str | None = None


@dataclass(slots=True, frozen=True)
class BroadcastResult:
    """Aggregate result of fan-out to many peers."""

    attempted: int
    succeeded: int
    failed: int
    results: tuple[DeliveryResult, ...] = field(default_factory=tuple)

    @property
    def all_ok(self) -> bool:
        return self.failed == 0 and self.attempted > 0


# ─── High-level typed inbound event ───────────────────────────────────────


@dataclass(slots=True, frozen=True)
class FederationEvent:
    """A fully-validated inbound event ready for the service layer.

    Produced by the federation service after envelope parse → timestamp skew
    check → instance lookup → ban check → Ed25519 verify → replay check →
    decrypt. Anything downstream of that validation sees this type, never the
    raw envelope.
    """

    msg_id: str
    event_type: FederationEventType
    from_instance: str
    to_instance: str
    timestamp: str
    payload: dict
    space_id: str | None = None
    epoch: int | None = None


# ─── GFS connection types (§24 — Global Federation Server) ──────────────


@dataclass(slots=True, frozen=True)
class GfsConnection:
    """A paired Global Federation Server connection."""

    id: str
    gfs_instance_id: str
    display_name: str
    public_key: str
    inbox_url: str
    status: str  # pending | active | suspended
    paired_at: str
    created_at: str | None = None


@dataclass(slots=True, frozen=True)
class GfsSpacePublication:
    """A space published to a GFS."""

    space_id: str
    gfs_connection_id: str
    published_at: str

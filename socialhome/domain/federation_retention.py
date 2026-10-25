"""§4.4.7 outbox retention policy — which queued federation events expire,
and when. NEVER_DROP events (security / structural state the receiver must
eventually see) never expire and retry indefinitely; all others get a 7-day
TTL from enqueue, after which the receiver rebuilds state via the sync
protocols rather than the outbox replaying a week-stale event. Call
negotiation (:data:`SHORT_LIVED`) gets two minutes instead, a moment's
delete (:data:`LONG_LIVED`, v_66) thirty days, and current state — online
status, a space-map pin — is queued latest-only (:func:`supersede_key`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .federation import FederationEventType
from .moment import MOMENT_MAX_LIFETIME_DAYS

#: Event types that must NEVER be marked ``failed`` regardless of attempt
#: count (§4.4.7).  These carry security or structural state — admin key
#: shares, bans, unpair signals, key revocations — that the receiver
#: must eventually see, even if the peer is offline for weeks.  Instead
#: of giving up after ``MAX_ATTEMPTS``, the OutboxProcessor keeps retrying
#: on the ceiling backoff (4 hours) indefinitely; these rows are enqueued
#: with ``expires_at = NULL`` so the retention sweep skips them too.
NEVER_DROP: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
        FederationEventType.SPACE_REMOTE_ADMIN_KICK,
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        FederationEventType.SPACE_ADMIN_KEY_SHARE,
        FederationEventType.SPACE_AUTHORITY_ROTATED,
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.UNPAIR,
    }
)

#: Hard ceiling on PENDING outbox rows per peer. When a peer is over this,
#: enqueue evicts its oldest *droppable* (non-NEVER_DROP) pending row before
#: inserting the new one, so a permanently-offline peer plus a busy space
#: can't flood the outbox. Ordinary events are best-effort (the receiver
#: rebuilds via sync), so dropping the oldest to keep the freshest state is
#: the right trade; NEVER_DROP rows are never evicted.
MAX_PENDING_PER_PEER = 1000

#: Ordinary (non-NEVER_DROP) outbox entries live this long before the prune
#: sweep marks them failed (§4.4.7).
RETENTION_DAYS = 7

#: How long a terminal (failed) outbox row is kept for operator diagnostics
#: before the prune sweep deletes it. Failures (give-up at MAX_ATTEMPTS, 4xx
#: PERMANENT, or expired-past-retention) flip the row to ``failed``; this
#: window lets an operator see recent delivery failures before they're purged.
TERMINAL_GRACE: timedelta = timedelta(hours=24)


#: Call negotiation (§26.8): a call rings for 90 s
#: (``call_service.RINGING_TTL_SECONDS``), so an offer, answer, candidate,
#: busy or decline still queued after a couple of minutes describes a call
#: that is already over — delivering it hours later only rings a dead call
#: or fills the peer's outbox slots. The ladder (5 / 10 / 20 / 40 s) still
#: gets four retries in. ``CALL_HANGUP`` / ``CALL_END`` keep the ordinary
#: TTL: one per call, and the far side's call record waits for it.
SHORT_LIVED: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.CALL_OFFER,
        FederationEventType.CALL_ANSWER,
        FederationEventType.CALL_DECLINE,
        FederationEventType.CALL_BUSY,
        FederationEventType.CALL_ICE,
        FederationEventType.CALL_ICE_CANDIDATE,
        FederationEventType.CALL_QUALITY,
    }
)

#: How long a :data:`SHORT_LIVED` entry stays queued.
SHORT_LIVED_TTL: timedelta = timedelta(minutes=2)

#: Online status transitions (``USER_ONLINE`` / ``USER_IDLE`` /
#: ``USER_OFFLINE``) — one person's current state, of which only the latest
#: is worth delivering.
_ONLINE_STATUS: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.USER_ONLINE,
        FederationEventType.USER_IDLE,
        FederationEventType.USER_OFFLINE,
    }
)


#: Events that must still arrive after a long absence, but not forever (v_66).
#: A moment lives up to 30 days at a follower (v_65) — and the next release
#: keeps saved copies past that — while the ordinary ladder gives up after
#: ~10 h, so a ``MOMENT_DELETED`` to a household offline for a weekend was
#: lost and the moment stayed on its screen. These keep retrying on the
#: ceiling backoff (like :data:`NEVER_DROP`) until :data:`LONG_LIVED_TTL`
#: from enqueue, and stay evictable under :data:`MAX_PENDING_PER_PEER`
#: (unlike it). Every relay hop enqueues its own copy, so a delete crosses
#: the 3-hop mesh even when the households between are offline for weeks.
LONG_LIVED: frozenset[FederationEventType] = frozenset(
    {FederationEventType.MOMENT_DELETED}
)

#: How long a :data:`LONG_LIVED` entry stays queued — a moment's longest
#: life.
LONG_LIVED_TTL: timedelta = timedelta(days=MOMENT_MAX_LIFETIME_DAYS)


def supersede_key(
    event_type: FederationEventType,
    *,
    space_id: str | None,
    payload: dict,
) -> str | None:
    """The latest-only key of an outbox entry, or ``None`` (kept as is).

    Some events carry *state*, not history: a person's online status, a
    person's pin on a space map. For those a newer send makes every queued
    older one worthless — and harmful: delivered after it, the older one
    overwrites the newer state at the peer, and every queued copy takes a
    slot of the per-peer cap that a DM or a post would otherwise keep. So
    the outbox holds at most one pending row per (peer, key): a new one
    replaces it, and a newer one delivered directly drops it.

    The key is local-only (an outbox column, never on the wire)."""
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    if event_type in _ONLINE_STATUS:
        return f"online:{user_id}"
    if event_type is FederationEventType.SPACE_LOCATION_UPDATED and space_id:
        return f"location:{space_id}:{user_id}"
    return None


def retention_expires_at(
    event_type: FederationEventType, *, now: datetime | None = None
) -> str | None:
    """ISO-8601 expiry for a freshly-enqueued outbox entry, or ``None`` for
    NEVER_DROP events (which retry indefinitely)."""
    if event_type in NEVER_DROP:
        return None
    base = now or datetime.now(UTC)
    if event_type in SHORT_LIVED:
        return (base + SHORT_LIVED_TTL).isoformat()
    if event_type in LONG_LIVED:
        return (base + LONG_LIVED_TTL).isoformat()
    return (base + timedelta(days=RETENTION_DAYS)).isoformat()

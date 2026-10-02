"""§4.4.7 outbox retention policy — which queued federation events expire,
and when. NEVER_DROP events (security / structural state the receiver must
eventually see) never expire and retry indefinitely; all others get a 7-day
TTL from enqueue, after which the receiver rebuilds state via the sync
protocols rather than the outbox replaying a week-stale event."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .federation import FederationEventType

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


def retention_expires_at(
    event_type: FederationEventType, *, now: datetime | None = None
) -> str | None:
    """ISO-8601 expiry for a freshly-enqueued outbox entry, or ``None`` for
    NEVER_DROP events (which retry indefinitely)."""
    if event_type in NEVER_DROP:
        return None
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(days=RETENTION_DAYS)).isoformat()

"""Federation outbox-entry domain type (§4.4.2)."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .federation import FederationEventType


@dataclass(slots=True, frozen=True)
class OutboxEntry:
    """One row of ``federation_outbox``."""

    id: str
    instance_id: str
    event_type: FederationEventType
    payload_json: str
    status: str  # "pending" | "delivered" | "failed"
    attempts: int
    next_attempt_at: str
    created_at: str
    authority_json: str | None = None
    expires_at: str | None = None
    delivered_at: str | None = None
    failed_at: str | None = None
    #: The local-only latest-only key (``supersede_key``); for a moment's
    #: delete also who signed it (``federation_retention.moment_delete_key``).
    supersede_key: str | None = None
    #: Migration 0145: the stored payload this row references
    #: (:class:`OutboxPayload`) — its envelope is sealed for this one peer
    #: when it is sent, and ``payload_json`` is ``''``. ``None`` for a row
    #: that carries its sealed envelope in ``payload_json`` (``queue_event``,
    #: and every row queued before 0145).
    payload_id: str | None = None
    #: The referenced payload's space (``None`` for a row without one) —
    #: what a gate needs without unwrapping the payload.
    space_id: str | None = None
    #: The referenced payload's ``msg_id`` — set only on a multi payload
    #: (:class:`OutboxPayload`), the one kind of row a multi upload carries.
    msg_id: str | None = None


@dataclass(slots=True, frozen=True)
class OutboxPayload:
    """One row of ``federation_outbox_payloads`` (migration 0145): an event's
    payload stored ONCE for every recipient it failed to reach.

    ``payload_enc`` is the payload JSON wrapped under the local KEK with
    ``id``, ``event_type`` and ``space_id`` as associated data. A failed
    send's payload has an HMAC ``id`` under a per-process key (one
    broadcast's failed recipients share the row, nothing guessable is
    stored) and no ``msg_id``. A *multi payload* — a broadcast to mailbox
    seats behind one GFS, uploaded once by the outbox (v_91) — has a random
    ``id`` and a uuid4 ``msg_id`` that every envelope built from it carries.
    """

    id: str
    event_type: FederationEventType
    space_id: str | None
    payload_enc: str
    msg_id: str | None = None


class DeliveryOutcome(enum.Enum):
    """Result of a single outbound delivery attempt.

    The processor uses the outcome to decide between three terminal
    paths:

    * :attr:`SUCCESS` — receiver returned 2xx. Row is marked delivered.
    * :attr:`PERMANENT` — receiver returned 4xx (already seen via
      replay cache, timestamp too old, banned, malformed envelope).
      Retrying will never succeed; row is marked failed immediately
      regardless of attempt count or :data:`~socialhome.domain.federation_retention.NEVER_DROP` membership.
    * :attr:`TRANSIENT` — 5xx, 429, timeout, DNS failure, connection
      reset. Row is rescheduled with jittered backoff, respecting
      :data:`~socialhome.domain.federation_retention.NEVER_DROP` past :data:`~socialhome.infrastructure.outbox_processor.MAX_ATTEMPTS`. A 429 that carries a
      ``Retry-After`` comes back as a :class:`RetryAfter` instead.
    """

    SUCCESS = "success"
    PERMANENT = "permanent"
    TRANSIENT = "transient"


@dataclass(slots=True, frozen=True)
class RetryAfter:
    """A :attr:`DeliveryOutcome.TRANSIENT` that names the earliest retry.

    Returned when the receiver throttled us (HTTP 429) and said how long
    its window has left (``Retry-After``). The processor treats it exactly
    like TRANSIENT — it costs an attempt, respects :data:`~socialhome.infrastructure.outbox_processor.MAX_ATTEMPTS` and
    :data:`~socialhome.domain.federation_retention.NEVER_DROP` — except that ``seconds`` is a floor on the next
    delay (capped at :data:`~socialhome.infrastructure.outbox_processor.MAX_RETRY_AFTER_S`, then jittered upward by up
    to :data:`~socialhome.infrastructure.outbox_processor.JITTER_RATIO`): never sooner than the peer asked, and spread so
    a burst it throttled does not return on one tick.
    """

    seconds: float

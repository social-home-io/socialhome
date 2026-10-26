"""Federation outbox-entry domain type (§4.4.2)."""

from __future__ import annotations

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

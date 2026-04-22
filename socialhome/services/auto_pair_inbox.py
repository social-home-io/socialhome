"""In-memory queue of incoming transitive auto-pair requests (§11).

Mirrors the shape of :class:`PairingRelayQueue` but for the *target*
side (C): when B forwards a signed introduction, we park the full
vouching envelope here so C's admin can click approve / decline.

Approval completes the pair instantly — the vouch signature already
attests that B has cryptographically verified both A and C, so the
usual QR + SAS step is redundant. The actual fast-path handshake is
performed by :class:`AutoPairCoordinator.finalize_pending`.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingAutoPairRequest:
    """Full envelope received from B. Kept in memory only — process
    restart discards pending requests, which matches the short-lived
    intent (admins act on inbox within minutes, not hours)."""

    request_id: str
    from_a_id: str
    from_a_pk: str
    from_a_webhook: str
    from_a_dh_pk: str
    via_b_id: str
    vouch_sig: str
    ts: str
    nonce: str
    token: str
    from_a_display: str = ""
    via_b_display: str = ""
    received_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


class AutoPairInbox:
    """Queue + subscribe hook for C-side admin approval."""

    __slots__ = ("_bus", "_items")

    def __init__(self, *, bus: EventBus) -> None:
        self._bus = bus
        self._items: dict[str, PendingAutoPairRequest] = {}

    def list_pending(self) -> list[PendingAutoPairRequest]:
        return sorted(
            self._items.values(),
            key=lambda r: r.received_at,
            reverse=True,
        )

    def get(self, request_id: str) -> PendingAutoPairRequest | None:
        return self._items.get(request_id)

    def pop(self, request_id: str) -> PendingAutoPairRequest | None:
        return self._items.pop(request_id, None)

    def enqueue(
        self,
        *,
        from_a_id: str,
        from_a_pk: str,
        from_a_webhook: str,
        from_a_dh_pk: str,
        via_b_id: str,
        vouch_sig: str,
        ts: str,
        nonce: str,
        token: str,
        from_a_display: str = "",
        via_b_display: str = "",
    ) -> PendingAutoPairRequest:
        request_id = secrets.token_urlsafe(16)
        req = PendingAutoPairRequest(
            request_id=request_id,
            from_a_id=from_a_id,
            from_a_pk=from_a_pk,
            from_a_webhook=from_a_webhook,
            from_a_dh_pk=from_a_dh_pk,
            via_b_id=via_b_id,
            vouch_sig=vouch_sig,
            ts=ts,
            nonce=nonce,
            token=token,
            from_a_display=from_a_display or from_a_id[:8],
            via_b_display=via_b_display or via_b_id[:8],
        )
        self._items[request_id] = req
        return req

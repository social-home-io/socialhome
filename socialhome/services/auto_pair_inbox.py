"""In-memory queue of incoming transitive auto-pair requests (§11).

Mirrors the shape of :class:`PairingRelayQueue` but for the *target*
side (C): when B forwards a signed introduction, we park the full
vouching envelope here so C's admin can click approve / decline.

Approval replaces the QR + SAS step — the vouch signature already
attests that B has cryptographically verified both A and C. It stores the
pair as pending and acks A; the pair is confirmed on C once A, having
confirmed on its side, sends its first authenticated envelope (see
:class:`AutoPairCoordinator.finalize_pending`). A request is kept for
:data:`AUTO_PAIR_REQUEST_TTL_SECONDS` and then forgotten.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..federation.inbound_validator import TIMESTAMP_SKEW_SECONDS
from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)

#: How long a trust-relay introduction stays answerable — kept in the
#: approval inbox on the target, approvable there (measured from the
#: requester's signed timestamp), and awaited by the requester.
AUTO_PAIR_REQUEST_TTL_SECONDS: int = 24 * 3600


def auto_pair_request_expired(iso_ts: str, *, now: datetime | None = None) -> bool:
    """Whether a request stamped *iso_ts* is older than the TTL — or dated
    further in the future than the federation clock-skew window
    (:data:`~socialhome.federation.inbound_validator.TIMESTAMP_SKEW_SECONDS`),
    which no real clock produces and which would otherwise never expire.
    An unparseable stamp counts as expired (fail closed)."""
    try:
        stamp = datetime.fromisoformat(iso_ts)
    except TypeError, ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    if stamp - now > timedelta(seconds=TIMESTAMP_SKEW_SECONDS):
        return True
    return now - stamp > timedelta(seconds=AUTO_PAIR_REQUEST_TTL_SECONDS)


@dataclass(slots=True)
class PendingAutoPairRequest:
    """Full envelope received from B. Kept in memory only — process
    restart discards pending requests — and for at most
    :data:`AUTO_PAIR_REQUEST_TTL_SECONDS`."""

    request_id: str
    from_a_id: str
    from_a_pk: str
    from_a_inbox_url: str
    from_a_dh_pk: str
    via_b_id: str
    vouch_sig: str
    ts: str
    nonce: str
    token: str
    from_a_display: str = ""
    via_b_display: str = ""
    received_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )


class AutoPairInbox:
    """Queue + subscribe hook for C-side admin approval."""

    __slots__ = ("_bus", "_items")

    def __init__(self, *, bus: EventBus) -> None:
        self._bus = bus
        self._items: dict[str, PendingAutoPairRequest] = {}

    def _prune(self) -> None:
        """Forget requests past :data:`AUTO_PAIR_REQUEST_TTL_SECONDS`."""
        for request_id in [
            rid
            for rid, req in self._items.items()
            if auto_pair_request_expired(req.received_at)
        ]:
            del self._items[request_id]

    def list_pending(self) -> list[PendingAutoPairRequest]:
        self._prune()
        return sorted(
            self._items.values(),
            key=lambda r: r.received_at,
            reverse=True,
        )

    def get(self, request_id: str) -> PendingAutoPairRequest | None:
        self._prune()
        return self._items.get(request_id)

    def pop(self, request_id: str) -> PendingAutoPairRequest | None:
        self._prune()
        return self._items.pop(request_id, None)

    def enqueue(
        self,
        *,
        from_a_id: str,
        from_a_pk: str,
        from_a_inbox_url: str,
        from_a_dh_pk: str,
        via_b_id: str,
        vouch_sig: str,
        ts: str,
        nonce: str,
        token: str,
        from_a_display: str = "",
        via_b_display: str = "",
    ) -> PendingAutoPairRequest:
        self._prune()
        request_id = secrets.token_urlsafe(16)
        req = PendingAutoPairRequest(
            request_id=request_id,
            from_a_id=from_a_id,
            from_a_pk=from_a_pk,
            from_a_inbox_url=from_a_inbox_url,
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

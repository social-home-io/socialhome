"""Pairing relay queue — in-memory store of pending §11.9 intro requests.

When a paired peer sends ``PAIRING_INTRO_RELAY`` the federation service
publishes :class:`PairingIntroRelayReceived`. This service subscribes,
stores the request, and exposes list/approve/decline for admins. On
approve we dispatch ``PAIRING_INTRO`` to the target instance; on
decline we drop it.

The queue is in-memory and capped — relay requests are operator-visible
but not durable: if the process restarts the requester can retry. A
later milestone could promote this to a DB-backed table if durability
becomes important.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import PairingIntroRelayReceived
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 100


@dataclass(slots=True, frozen=True)
class PendingRelayRequest:
    id: str
    from_instance: str
    target_instance_id: str
    message: str
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PairingRelayQueue:
    """In-memory queue of admin-pending pairing relay requests."""

    __slots__ = ("_bus", "_federation", "_items", "_own_instance_id")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation: "FederationService",
        own_instance_id: str,
    ) -> None:
        self._bus = bus
        self._federation = federation
        self._items: dict[str, PendingRelayRequest] = {}
        self._own_instance_id = own_instance_id

    def wire(self) -> None:
        """Subscribe to PairingIntroRelayReceived on the bus. Idempotent."""
        self._bus.subscribe(PairingIntroRelayReceived, self._on_relay_received)

    async def _on_relay_received(self, event: PairingIntroRelayReceived) -> None:
        request_id = uuid.uuid4().hex
        self._items[request_id] = PendingRelayRequest(
            id=request_id,
            from_instance=event.from_instance,
            target_instance_id=event.target_instance_id,
            message=event.message,
        )
        # Drop oldest entries past the cap so a noisy peer cannot DoS the list.
        if len(self._items) > _MAX_QUEUE_SIZE:
            oldest = min(self._items.values(), key=lambda r: r.received_at)
            self._items.pop(oldest.id, None)

    def list_pending(self) -> list[PendingRelayRequest]:
        return sorted(self._items.values(), key=lambda r: r.received_at)

    def get(self, request_id: str) -> PendingRelayRequest | None:
        return self._items.get(request_id)

    async def approve(self, request_id: str) -> PendingRelayRequest | None:
        """Forward PAIRING_INTRO to the target instance. Returns the request
        on success, or None if the id was unknown."""
        pending = self._items.pop(request_id, None)
        if pending is None:
            return None
        result = await self._federation.send_event(
            to_instance_id=pending.target_instance_id,
            event_type=FederationEventType.PAIRING_INTRO,
            payload={
                "via_instance_id": pending.from_instance,
                "message": pending.message,
            },
        )
        if not result.ok:
            log.warning(
                "PAIRING_INTRO relay to %s failed — request dropped",
                pending.target_instance_id,
            )
        return pending

    def decline(self, request_id: str) -> PendingRelayRequest | None:
        """Drop the request with no outbound traffic. Returns the request
        on success, or None if the id was unknown."""
        return self._items.pop(request_id, None)

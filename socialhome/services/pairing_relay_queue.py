"""Pairing relay queue — durable §11.9 ``PAIRING_INTRO_RELAY`` queue.

When a paired peer sends ``PAIRING_INTRO_RELAY`` the federation service
publishes :class:`PairingIntroRelayReceived`. This service subscribes,
persists the request via :class:`AbstractPairingRelayRepo`, and exposes
list / approve / decline for admins. On approve we dispatch
``PAIRING_INTRO`` to the target instance; on decline we mark the row
declined (kept for audit; the §11.9 retention sweeper drops it later).

Promoted from in-memory to SQLite-backed so a restart no longer loses
the admin queue.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import PairingIntroRelayReceived
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from ..repositories.pairing_relay_repo import AbstractPairingRelayRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)

#: Cap on pending rows. A noisy peer that floods us is bounded by this
#: count — older pending rows are deleted as new ones arrive.
_MAX_PENDING_ROWS = 100


@dataclass(slots=True, frozen=True)
class PendingRelayRequest:
    id: str
    from_instance: str
    target_instance_id: str
    message: str
    received_at: datetime


def _row_to_request(row: dict) -> PendingRelayRequest:
    return PendingRelayRequest(
        id=row["id"],
        from_instance=row["from_instance"],
        target_instance_id=row["target_instance_id"],
        message=row["message"],
        received_at=datetime.fromisoformat(row["received_at"]),
    )


class PairingRelayQueue:
    """Durable queue of admin-pending pairing relay requests."""

    __slots__ = ("_bus", "_federation", "_repo", "_own_instance_id")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation: "FederationService",
        repo: AbstractPairingRelayRepo,
        own_instance_id: str,
    ) -> None:
        self._bus = bus
        self._federation = federation
        self._repo = repo
        self._own_instance_id = own_instance_id

    def wire(self) -> None:
        """Subscribe to PairingIntroRelayReceived on the bus. Idempotent."""
        self._bus.subscribe(PairingIntroRelayReceived, self._on_relay_received)

    async def _on_relay_received(self, event: PairingIntroRelayReceived) -> None:
        request_id = uuid.uuid4().hex
        await self._repo.save(
            request_id=request_id,
            from_instance=event.from_instance,
            target_instance_id=event.target_instance_id,
            message=event.message,
            received_at=datetime.now(timezone.utc),
        )
        # Drop oldest pending rows past the cap so a noisy peer cannot
        # DoS the admin list.
        if await self._repo.count_pending() > _MAX_PENDING_ROWS:
            await self._repo.delete_oldest_pending(_MAX_PENDING_ROWS)

    async def list_pending(self) -> list[PendingRelayRequest]:
        rows = await self._repo.list_pending()
        return [_row_to_request(r) for r in rows]

    async def get(self, request_id: str) -> PendingRelayRequest | None:
        row = await self._repo.get(request_id)
        return _row_to_request(row) if row else None

    async def approve(self, request_id: str) -> PendingRelayRequest | None:
        """Forward PAIRING_INTRO to the target instance. Returns the request
        on success, or None if the id was unknown."""
        row = await self._repo.get(request_id)
        if row is None:
            return None
        pending = _row_to_request(row)
        result = await self._federation.send_event(
            to_instance_id=pending.target_instance_id,
            event_type=FederationEventType.PAIRING_INTRO,
            payload={
                "via_instance_id": pending.from_instance,
                "message": pending.message,
            },
        )
        await self._repo.set_status(request_id, "approved")
        if not result.ok:
            log.warning(
                "PAIRING_INTRO relay to %s failed — request marked approved",
                pending.target_instance_id,
            )
        return pending

    async def decline(self, request_id: str) -> PendingRelayRequest | None:
        """Mark the request declined. No outbound traffic. Returns the
        request on success, or None if the id was unknown."""
        row = await self._repo.get(request_id)
        if row is None:
            return None
        await self._repo.set_status(request_id, "declined")
        return _row_to_request(row)

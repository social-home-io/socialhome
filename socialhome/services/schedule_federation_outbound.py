"""Outbound federation for space-scoped schedule polls (§9 / §13).

Subscribes to :class:`SchedulePollResponded` and
:class:`SchedulePollFinalized` domain events; when the event carries
a ``space_id``, fans out the matching ``SPACE_SCHEDULE_*``
federation events to every peer instance that's a member of the
space.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    SchedulePollCreated,
    SchedulePollFinalized,
    SchedulePollResponded,
)
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from .moderation_release import with_release

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


class ScheduleFederationOutbound:
    """Publish schedule-poll mutations to paired peer instances."""

    __slots__ = ("_bus", "_federation")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
    ) -> None:
        self._bus = bus
        self._federation = federation_service

    def wire(self) -> None:
        self._bus.subscribe(SchedulePollCreated, self._on_created)
        self._bus.subscribe(SchedulePollResponded, self._on_responded)
        self._bus.subscribe(SchedulePollFinalized, self._on_finalized)

    async def _on_created(self, event: SchedulePollCreated) -> None:
        """F5: broadcast slot defs + title + deadline so a remote
        member can render the slot picker on arrival."""
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_SCHEDULE_CREATED,
            # A schedule riding a reviewed post is published by its
            # approval, which it names (v_43).
            with_release(
                {
                    "post_id": event.post_id,
                    "space_id": event.space_id,
                    "title": event.title,
                    "deadline": event.deadline,
                    "slots": list(event.slots),
                }
            ),
        )

    async def _on_responded(self, event: SchedulePollResponded) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED,
            {
                "post_id": event.post_id,
                "slot_id": event.slot_id,
                "user_id": event.user_id,
                "response": event.response,
                "space_id": event.space_id,
            },
        )

    async def _on_finalized(self, event: SchedulePollFinalized) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_SCHEDULE_FINALIZED,
            {
                "post_id": event.post_id,
                "slot_id": event.slot_id,
                "slot_date": event.slot_date,
                "start_time": event.start_time,
                "end_time": event.end_time,
                "title": event.title,
                "finalized_by": event.finalized_by,
                "space_id": event.space_id,
            },
        )

    async def _fan_out(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                event_type,
                payload,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "schedule-outbound: broadcast failed for space=%s: %s",
                space_id,
                exc,
            )

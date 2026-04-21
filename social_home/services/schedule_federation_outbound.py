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

from ..domain.events import SchedulePollFinalized, SchedulePollResponded
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class ScheduleFederationOutbound:
    """Publish schedule-poll mutations to paired peer instances."""

    __slots__ = ("_bus", "_federation", "_space_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._space_repo = space_repo

    def wire(self) -> None:
        self._bus.subscribe(SchedulePollResponded, self._on_responded)
        self._bus.subscribe(SchedulePollFinalized, self._on_finalized)

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
            peers = await self._space_repo.list_member_instances(space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("schedule-outbound: list peers failed: %s", exc)
            return
        own = getattr(self._federation, "_own_instance_id", "")
        for instance_id in peers:
            if instance_id == own or not instance_id:
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=event_type,
                    payload=payload,
                    space_id=space_id,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "schedule-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )

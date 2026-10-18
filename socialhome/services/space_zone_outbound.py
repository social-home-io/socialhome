"""Outbound federation for per-space zone CRUD (§23.8.7).

Subscribes to :class:`SpaceZoneUpserted` / :class:`SpaceZoneDeleted`
domain events emitted by :class:`SpaceZoneService` and fans them out
as sealed ``SPACE_ZONE_UPSERTED`` / ``SPACE_ZONE_DELETED`` federation
events to every remote instance that has at least one member in the
space. Each remote instance applies the upsert / delete to its local
``space_zones`` table via the matching inbound handler in
:mod:`federation_inbound.space_content`.

Mirrors the pattern of :class:`TaskFederationOutbound` — no business
logic, just translate domain events into wire envelopes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import SpaceZoneDeleted, SpaceZoneUpserted
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from .space_writer_households import SpaceWriterHouseholds

log = logging.getLogger(__name__)


class SpaceZoneOutbound:
    """Publish per-space zone mutations to remote member instances."""

    __slots__ = ("_bus", "_federation", "_members")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        member_households: SpaceWriterHouseholds,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        #: Zones are the members' places — never sent to a household that
        #: only follows the space (owner decision 13).
        self._members = member_households

    def wire(self) -> None:
        self._bus.subscribe(SpaceZoneUpserted, self._on_upserted)
        self._bus.subscribe(SpaceZoneDeleted, self._on_deleted)

    async def _on_upserted(self, event: SpaceZoneUpserted) -> None:
        if event.origin_instance_id is not None:
            return  # applied from another household — never echo it back
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_ZONE_UPSERTED,
            {
                "zone_id": event.zone_id,
                "space_id": event.space_id,
                "name": event.name,
                "latitude": event.latitude,
                "longitude": event.longitude,
                "radius_m": event.radius_m,
                "color": event.color,
                "icon": event.icon,
                "created_by": event.created_by,
                "updated_at": event.updated_at,
            },
        )

    async def _on_deleted(self, event: SpaceZoneDeleted) -> None:
        if event.origin_instance_id is not None:
            return  # applied from another household — never echo it back
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_ZONE_DELETED,
            {
                "zone_id": event.zone_id,
                "space_id": event.space_id,
                "deleted_by": event.deleted_by,
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
                only_instances=await self._members.writer_households(space_id),
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "zone-outbound: broadcast failed for space=%s: %s",
                space_id,
                exc,
                exc_info=True,
            )

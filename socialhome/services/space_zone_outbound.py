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
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class SpaceZoneOutbound:
    """Publish per-space zone mutations to remote member instances."""

    __slots__ = ("_bus", "_federation", "_spaces")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._spaces = space_repo

    def wire(self) -> None:
        self._bus.subscribe(SpaceZoneUpserted, self._on_upserted)
        self._bus.subscribe(SpaceZoneDeleted, self._on_deleted)

    async def _on_upserted(self, event: SpaceZoneUpserted) -> None:
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
                "created_by": event.created_by,
                "updated_at": event.updated_at,
            },
        )

    async def _on_deleted(self, event: SpaceZoneDeleted) -> None:
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
            peers = await self._spaces.list_member_instances(space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("zone-outbound: list peers failed: %s", exc)
            return
        own = getattr(self._federation, "_own_instance_id", "")
        for instance_id in peers:
            if not instance_id or instance_id == own:
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
                    "zone-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )

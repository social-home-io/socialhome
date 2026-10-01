"""Outbound federation for space timetables (v_39).

Subscribes to :class:`TimetableSaved` / :class:`TimetableDeleted` and fans
a **space** timetable out to the space's member households as
``SPACE_TIMETABLE_UPSERTED`` / ``SPACE_TIMETABLE_DELETED``. Household
timetables (``space_id is None``) never leave the house, and an event
that arrived over federation (``origin_instance_id`` set) is never
re-broadcast — the originating household already fanned it out.

Delivery is :meth:`FederationService.broadcast_to_space_members` only:
``space_instances`` names the households with at least one member, so a
household on the mesh that is not a member never receives one (and a
path through such a relay is sealed end-to-end as ``SPACE_ROUTED``).
The envelope's routing fields — event type, from / to instance,
``space_id`` — are the only plaintext; the whole payload (the full
timetable wire dict, ``updated_by`` included, or the delete's id / time /
admin / creator) is sealed with the per-peer session key like every space event.
Households below :attr:`FederationCapability.MIN_FOR_SPACE_TIMETABLE`
are skipped: they have no Timetable tab to show it in.

Size: :func:`socialhome.domain.timetable.validate` caps a timetable at
96 KiB of wire JSON before it can be saved, so a sealed upsert — and the
twice-sealed sync chunk of the same timetable — stays inside the
connection-server relay's envelope cap for link-joined members (pinned by
tests).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..domain.events import TimetableDeleted, TimetableSaved
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.timetable import Timetable, to_wire_dict
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


def upsert_payload(tt: Timetable, space_id: str) -> dict[str, Any]:
    """The ``SPACE_TIMETABLE_UPSERTED`` payload (sealed on the wire)."""
    return {"space_id": space_id, "timetable": to_wire_dict(tt)}


def delete_payload(event: TimetableDeleted, space_id: str) -> dict[str, Any]:
    """The ``SPACE_TIMETABLE_DELETED`` payload (sealed on the wire)."""
    return {
        "space_id": space_id,
        "timetable_id": event.timetable_id,
        "deleted_at": event.occurred_at.isoformat(),
        "deleted_by": event.deleted_by,
        # Lets a receiver that never held the id check it commits to this
        # creator in this space before it files a tombstone for it.
        "created_by": event.created_by,
    }


class TimetableFederationOutbound:
    """Publish local space-timetable edits to the space's member households."""

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
        self._bus.subscribe(TimetableSaved, self._on_saved)
        self._bus.subscribe(TimetableDeleted, self._on_deleted)

    async def _on_saved(self, event: TimetableSaved) -> None:
        if event.space_id is None or event.origin_instance_id is not None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TIMETABLE_UPSERTED,
            upsert_payload(event.timetable, event.space_id),
        )

    async def _on_deleted(self, event: TimetableDeleted) -> None:
        if event.space_id is None or event.origin_instance_id is not None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TIMETABLE_DELETED,
            delete_payload(event, event.space_id),
        )

    async def _fan_out(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict[str, Any],
    ) -> None:
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                event_type,
                payload,
                min_proto_version=FederationCapability.MIN_FOR_SPACE_TIMETABLE,
            )
        except Exception as exc:  # a failed fan-out never fails the edit
            log.warning(
                "timetable-outbound: %s broadcast failed for space=%s: %s",
                event_type.value,
                space_id,
                exc,
            )

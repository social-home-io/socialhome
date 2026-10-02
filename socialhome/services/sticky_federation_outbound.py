"""Outbound federation for space-scoped stickies (§19 / §13).

Subscribes to :class:`StickyCreated` / :class:`StickyUpdated` /
:class:`StickyDeleted` domain events and — when the sticky carries a
``space_id`` — fans out the matching ``SPACE_STICKY_*`` federation
event to every peer instance that's a member of the space.

Per-event push complements the snapshot-sync scheduler
(``federation/sync/space/``): subscribers still receive a full sticky
snapshot on their next sync tick, but between ticks they see changes
in near real-time.

Household-scoped stickies (``space_id is None``) stay local — no peer
has a right to know about them.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from ..domain.events import StickyCreated, StickyDeleted, StickyUpdated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from .moderation_release import with_release

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


class StickyFederationOutbound:
    """Publish space-scoped sticky mutations to paired peer instances."""

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
        """Subscribe handlers on the bus. Idempotent."""
        self._bus.subscribe(StickyCreated, self._on_created)
        self._bus.subscribe(StickyUpdated, self._on_updated)
        self._bus.subscribe(StickyDeleted, self._on_deleted)

    async def _on_created(self, event: StickyCreated) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_STICKY_CREATED,
            _payload_from_created(event),
        )

    async def _on_updated(self, event: StickyUpdated) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_STICKY_UPDATED,
            _payload_from_updated(event),
        )

    async def _on_deleted(self, event: StickyDeleted) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_STICKY_DELETED,
            _drop_blank_actor(
                {
                    "id": event.sticky_id,
                    "space_id": event.space_id,
                    "actor_user_id": event.actor_user_id,
                }
            ),
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
                "sticky-outbound: broadcast failed for space=%s: %s",
                space_id,
                exc,
            )


def _payload_from_created(event: StickyCreated) -> dict:
    d = asdict(event)
    d.pop("occurred_at", None)
    d["id"] = d.pop("sticky_id")
    return _drop_blank_actor(d)


def _payload_from_updated(event: StickyUpdated) -> dict:
    d = asdict(event)
    d.pop("occurred_at", None)
    d["id"] = d.pop("sticky_id")
    return _drop_blank_actor(d)


def _drop_blank_actor(payload: dict) -> dict:
    """``actor_user_id`` (v_42) rides only when known — who made the write,
    which receivers check against the space's ``stickies`` access level."""
    if not payload.get("actor_user_id"):
        payload.pop("actor_user_id", None)
    # A write released from the moderation queue names the release (v_43).
    return with_release(payload)

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

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class StickyFederationOutbound:
    """Publish space-scoped sticky mutations to paired peer instances."""

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
            {"id": event.sticky_id, "space_id": event.space_id},
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
            log.debug("sticky-outbound: list peers failed: %s", exc)
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
                    "sticky-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )


def _payload_from_created(event: StickyCreated) -> dict:
    d = asdict(event)
    d.pop("occurred_at", None)
    d["id"] = d.pop("sticky_id")
    return d


def _payload_from_updated(event: StickyUpdated) -> dict:
    d = asdict(event)
    d.pop("occurred_at", None)
    d["id"] = d.pop("sticky_id")
    return d

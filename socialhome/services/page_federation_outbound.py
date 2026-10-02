"""Outbound federation for space-scoped pages (§13 / §14).

Subscribes to :class:`PageCreated` / :class:`PageUpdated` /
:class:`PageDeleted` domain events. When the event carries a
``space_id``, broadcasts the matching ``SPACE_PAGE_*`` federation
event to every member household via mesh-routed
``broadcast_to_space_members``. Without this service, page edits
stayed purely local — the inbound handler in
:mod:`federation_inbound.space_content` existed but had no
matching outbound producer, so a remote member saw stale wiki
content until the next §25.6 catch-up sync.

Household-scoped pages (``space_id is None``) stay local — no peer
has a right to know about them.

Every payload carries ``actor_user_id`` (v_42) — who made the write, which
receivers check against the space's ``pages`` access level — and a create
also its ``created_by`` (the same user): the page id is owner-bound to its
creator, so a receiver can only file a new page it can attribute. A write
released from the moderation queue adds the ``moderation`` approval block
(v_43, :mod:`.moderation_release`).

Sibling of :class:`TaskFederationOutbound` /
:class:`StickyFederationOutbound`; identical shape, different
event types.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import PageCreated, PageDeleted, PageUpdated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from .moderation_release import with_release

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


class PageFederationOutbound:
    """Publish space-scoped page mutations to paired peer instances."""

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
        self._bus.subscribe(PageCreated, self._on_created)
        self._bus.subscribe(PageUpdated, self._on_updated)
        self._bus.subscribe(PageDeleted, self._on_deleted)

    async def _on_created(self, event: PageCreated) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_PAGE_CREATED,
            _with_actor(
                {
                    "id": event.page_id,
                    "page_id": event.page_id,
                    "space_id": event.space_id,
                    "title": event.title,
                    "content": event.content,
                },
                event.actor_user_id,
                creates=True,
            ),
        )

    async def _on_updated(self, event: PageUpdated) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_PAGE_UPDATED,
            _with_actor(
                {
                    "id": event.page_id,
                    "page_id": event.page_id,
                    "space_id": event.space_id,
                    "title": event.title,
                    "content": event.content,
                },
                event.actor_user_id,
            ),
        )

    async def _on_deleted(self, event: PageDeleted) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_PAGE_DELETED,
            _with_actor(
                {
                    "id": event.page_id,
                    "page_id": event.page_id,
                    "space_id": event.space_id,
                },
                event.actor_user_id,
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
                "page-outbound: broadcast failed for space=%s: %s",
                space_id,
                exc,
            )


def _with_actor(payload: dict, actor_user_id: str, *, creates: bool = False) -> dict:
    """``payload`` plus the write's actor (v_42) — and, for a create, the
    creator it is attributed to — when known, and the approval block of a
    moderation release (v_43)."""
    if actor_user_id:
        payload["actor_user_id"] = actor_user_id
        if creates:
            payload["created_by"] = actor_user_id
    return with_release(payload)

"""Outbound federation for space-scoped comments (§13).

Subscribes to :class:`CommentAdded` / :class:`CommentUpdated` /
:class:`CommentDeleted` events and — when the comment carries a
``space_id`` — fans out the matching ``SPACE_COMMENT_*`` federation
event to every peer instance that's a member of the space.

Household-scoped comments (``space_id is None``) stay local.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import CommentAdded, CommentDeleted, CommentUpdated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class CommentFederationOutbound:
    """Publish space-scoped comment mutations to peer instances."""

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
        self._bus.subscribe(CommentAdded, self._on_added)
        self._bus.subscribe(CommentUpdated, self._on_updated)
        self._bus.subscribe(CommentDeleted, self._on_deleted)

    async def _on_added(self, event: CommentAdded) -> None:
        if event.space_id is None:
            return
        c = event.comment
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_COMMENT_CREATED,
            {
                "id": c.id,
                "post_id": c.post_id,
                "author": c.author,
                "parent_id": c.parent_id,
                "content": c.content,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "space_id": event.space_id,
            },
        )

    async def _on_updated(self, event: CommentUpdated) -> None:
        if event.space_id is None:
            return
        c = event.comment
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_COMMENT_UPDATED,
            {
                "id": c.id,
                "post_id": c.post_id,
                "content": c.content,
                "edited_at": c.edited_at.isoformat() if c.edited_at else None,
                "space_id": event.space_id,
            },
        )

    async def _on_deleted(self, event: CommentDeleted) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_COMMENT_DELETED,
            {
                "id": event.comment_id,
                "post_id": event.post_id,
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
            log.debug("comment-outbound: list peers failed: %s", exc)
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
                    "comment-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )

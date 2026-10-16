"""Outbound federation for space-scoped polls (§9 / §13).

Mirrors the sticky/task/schedule pattern — subscribes to
:class:`PollCreated` / :class:`PollVoted` / :class:`PollClosed` and
fans out ``SPACE_POLL_CREATED`` / ``SPACE_POLL_VOTE_CAST`` /
``SPACE_POLL_CLOSED`` federation events to every peer instance in the
owning space.

Household-scoped polls stay local — no peer has a right to see them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import PollClosed, PollCreated, PollVoted
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


class PollFederationOutbound:
    """Publish reply-poll mutations to paired peer instances."""

    __slots__ = ("_bus", "_federation")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
    ) -> None:
        self._bus = bus
        self._federation = federation_service

    def wire(self) -> None:
        self._bus.subscribe(PollCreated, self._on_created)
        self._bus.subscribe(PollVoted, self._on_voted)
        self._bus.subscribe(PollClosed, self._on_closed)

    async def _on_created(self, event: PollCreated) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_POLL_CREATED,
            {
                "post_id": event.post_id,
                "question": event.question,
                "allow_multiple": event.allow_multiple,
                "space_id": event.space_id,
            },
        )

    async def _on_voted(self, event: PollVoted) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_POLL_VOTE_CAST,
            {
                "post_id": event.post_id,
                "voter_user_id": event.voter_user_id,
                "option_ids": list(event.option_ids),
                "space_id": event.space_id,
            },
        )

    async def _on_closed(self, event: PollClosed) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_POLL_CLOSED,
            {"post_id": event.post_id, "space_id": event.space_id},
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
                "poll-outbound: broadcast failed for space=%s: %s",
                space_id,
                exc,
                exc_info=True,
            )

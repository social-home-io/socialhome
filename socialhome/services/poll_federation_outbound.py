"""Outbound federation for space-scoped polls (§9 / §13).

Mirrors the sticky/task/schedule pattern — subscribes to
:class:`PollCreated` / :class:`PollVoted` / :class:`PollClosed` and
fans out ``SPACE_POLL_CREATED`` / ``SPACE_POLL_VOTE_CAST`` /
``SPACE_POLL_CLOSED`` federation events to every peer instance in the
owning space.

``SPACE_POLL_CREATED`` carries the whole poll definition (question,
options with their ids, ``allow_multiple``, ``closes_at``): the poll is
created after its post on every path (with the post, attached later, on a
moderation approval), so the post's own event cannot carry it.
``SPACE_POLL_VOTE_CAST`` carries the voter's whole selection
(``option_ids``; empty = retracted) and, since v_94, the stamp it was made
at (``at``). Receivers merge by the stamp
(:mod:`~socialhome.domain.item_merge`), so the order of arrival does not
matter — until the poll closes: then nothing counts any more.

Household-scoped polls stay local — no peer has a right to see them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import PollClosed, PollCreated, PollVoted
from ..domain.federation import FederationEventType
from ..domain.post import poll_to_wire_dict
from ..infrastructure.event_bus import EventBus
from .moderation_release import with_release

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


def _stamped(payload: dict, at: str | None) -> dict:
    """``payload`` with the change's stamp (``at``) when there is one."""
    return {**payload, "at": at} if at else payload


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
        if event.space_id is None or event.origin_instance_id is not None:
            return  # household-only, or a peer's poll: never echoed back
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_POLL_CREATED,
            # The whole definition — the option ids are the ones every
            # household's votes name. A poll riding a reviewed post is
            # published by its approval, which it names (v_43).
            with_release(
                poll_to_wire_dict(
                    post_id=event.post_id,
                    space_id=event.space_id,
                    question=event.question,
                    options=event.options,
                    allow_multiple=event.allow_multiple,
                    closes_at=event.closes_at,
                )
            ),
        )

    async def _on_voted(self, event: PollVoted) -> None:
        if event.space_id is None or event.origin_instance_id is not None:
            return  # household-only, or a peer's vote: never echoed back
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_POLL_VOTE_CAST,
            _stamped(
                {
                    "post_id": event.post_id,
                    "voter_user_id": event.voter_user_id,
                    "option_ids": list(event.option_ids),
                    "space_id": event.space_id,
                },
                event.at,
            ),
        )

    async def _on_closed(self, event: PollClosed) -> None:
        if event.space_id is None or event.origin_instance_id is not None:
            return  # household-only, or a peer's close: never echoed back
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

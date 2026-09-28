"""Outbound federation for user status changes (emoji + text).

Subscribes to :class:`UserStatusChanged` and sends ``USER_STATUS_UPDATED``
to every confirmed social peer, like :class:`ProfileFederationOutbound`
does for ``USER_UPDATED``. The payload rides the pairwise-encrypted
envelope; it carries ``{user_id, emoji, text, expires_at}`` or
``{user_id, status_cleared: true}`` — the shape the inbound handler in
``FederationInboundService._on_user_status_updated`` already parses.

Only statuses of users homed here go out: the inbound handler re-publishes
``UserStatusChanged`` for a *remote* user's status, and echoing that back
into the mesh would have us speak for another household's user (which the
receivers refuse anyway, see ``docs/protocol/presence.md``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import UserStatusChanged
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from .peer_outbound import ConfirmedPeerBroadcaster, SingleTargetSender
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class UserStatusOutbound(
    VisibilityMixin,
    ConfirmedPeerBroadcaster,
    SingleTargetSender,
):
    """Publish local :class:`UserStatusChanged` events as ``USER_STATUS_UPDATED``."""

    _federation: "FederationService"

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_user_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        user_repo: "AbstractUserRepo",
        visibility_repo: "AbstractPeerUserVisibilityRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._user_repo = user_repo
        self._visibility_repo = visibility_repo

    def wire(self) -> None:
        self._bus.subscribe(UserStatusChanged, self._on_changed)

    async def _on_changed(self, event: UserStatusChanged) -> None:
        # ``users`` holds local users only — a remote user's status
        # (re-published by the inbound handler) is not ours to send.
        if await self._user_repo.get_by_user_id(event.user_id) is None:
            return
        status = event.status
        payload: dict
        if status is None or not status.is_set:
            payload = {"user_id": event.user_id, "status_cleared": True}
        else:
            payload = {
                "user_id": event.user_id,
                "emoji": status.emoji,
                "text": status.text,
                "expires_at": status.expires_at,
            }
        for instance_id in await self.list_confirmed_peer_ids():
            # Per-pair user-visibility filter: a user hidden from this
            # peer never shows up there, status included.
            if event.user_id in await self.hidden_for_peer(instance_id):
                continue
            await self.send_to_instance(
                instance_id,
                FederationEventType.USER_STATUS_UPDATED,
                payload,
            )

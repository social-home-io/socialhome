"""Outbound federation for per-space member profile updates (§13).

Subscribes to :class:`SpaceMemberProfileUpdated` and fans out
``SPACE_MEMBER_PROFILE_UPDATED`` to every paired peer that's a member
of the space. Carries the WebP base64 bytes when the picture changes;
display-name-only changes ship the hash alone.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from ..domain.events import SpaceMemberProfileUpdated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class SpaceMemberProfileFederationOutbound:
    """Fan per-space profile mutations out to federated member instances."""

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
        self._bus.subscribe(
            SpaceMemberProfileUpdated,
            self._on_updated,
        )

    async def _on_updated(
        self,
        event: SpaceMemberProfileUpdated,
    ) -> None:
        payload: dict = {
            "space_id": event.space_id,
            "user_id": event.user_id,
            "space_display_name": event.space_display_name,
            "picture_hash": event.picture_hash,
        }
        if event.picture_webp is not None:
            payload["picture_webp_base64"] = base64.b64encode(
                event.picture_webp,
            ).decode("ascii")
        try:
            peers = await self._space_repo.list_member_instances(
                event.space_id,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("sm-profile-outbound: list peers failed: %s", exc)
            return
        own = getattr(self._federation, "_own_instance_id", "")
        for instance_id in peers:
            if not instance_id or instance_id == own:
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=(FederationEventType.SPACE_MEMBER_PROFILE_UPDATED),
                    payload=payload,
                    space_id=event.space_id,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "sm-profile-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )

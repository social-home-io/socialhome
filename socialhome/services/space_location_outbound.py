"""Per-space location fan-out (§23.8.6).

Subscribes to :class:`PresenceUpdated` and, for every space where the
user has opted in (``location_share_enabled = 1``) AND the space has
the map feature on (``feature_location = 1``), publishes:

1. A local WebSocket frame ``space_location_updated`` to every space
   member's session on this instance.
2. A sealed ``SPACE_LOCATION_UPDATED`` federation event to every
   remote instance that has at least one member in this space.

Every space-bound payload carries **GPS only** — ``zone_name`` is
deliberately stripped at the household boundary so HA-defined zone
names never reach a space (§23.8.5, §23.8.6, §25.10.1). When the
accuracy gate has nulled ``lat``/``lon`` we skip both fan-outs
entirely; the household dashboard still gets ``presence.updated`` on
its own channel.

This is the space twin of the household ``presence.updated`` frame
emitted by :class:`RealtimeService`. Two different ``type`` strings,
two different consumers, one PresenceUpdated event.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import PresenceUpdated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from ..infrastructure.ws_manager import WebSocketManager
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


class SpaceLocationOutbound:
    """Fan a household :class:`PresenceUpdated` out to every opted-in
    space, both locally (WS) and to remote member instances
    (federation).
    """

    __slots__ = ("_bus", "_ws", "_federation", "_spaces", "_users")

    def __init__(
        self,
        *,
        bus: EventBus,
        ws: WebSocketManager,
        federation_service: "FederationService",
        space_repo: AbstractSpaceRepo,
        user_repo: AbstractUserRepo,
    ) -> None:
        self._bus = bus
        self._ws = ws
        self._federation = federation_service
        self._spaces = space_repo
        self._users = user_repo

    def wire(self) -> None:
        self._bus.subscribe(PresenceUpdated, self._on_presence_updated)

    async def _on_presence_updated(self, event: PresenceUpdated) -> None:
        # Skip when the accuracy gate dropped coordinates — there is
        # nothing useful to put on a GPS-only space channel. Members
        # without coordinates remain visible on the household
        # dashboard via ``presence.updated`` (zone-name only).
        if event.latitude is None or event.longitude is None:
            return

        user = await self._users.get(event.username)
        if user is None:
            return

        spaces = await self._spaces.list_location_shared_spaces_for_user(
            user.user_id,
        )
        if not spaces:
            return

        for space in spaces:
            payload = _gps_payload(
                space_id=space.id,
                user_id=user.user_id,
                latitude=event.latitude,
                longitude=event.longitude,
                accuracy_m=event.gps_accuracy_m,
                updated_at=event.updated_at,
            )
            await self._broadcast_local(space.id, payload)
            await self._fan_out_federation(space.id, payload)

    # ── Fan-out helpers ────────────────────────────────────────────────

    async def _broadcast_local(self, space_id: str, payload: dict) -> None:
        try:
            user_ids = await self._spaces.list_local_member_user_ids(space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "space-location-outbound: list local members failed: %s",
                exc,
            )
            return
        await self._ws.broadcast_to_users(
            user_ids,
            {"type": "space_location_updated", "data": payload},
        )

    async def _fan_out_federation(
        self,
        space_id: str,
        payload: dict,
    ) -> None:
        try:
            peers = await self._spaces.list_member_instances(space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "space-location-outbound: list peers failed: %s",
                exc,
            )
            return
        own = getattr(self._federation, "_own_instance_id", "")
        for instance_id in peers:
            if not instance_id or instance_id == own:
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=FederationEventType.SPACE_LOCATION_UPDATED,
                    payload=payload,
                    space_id=space_id,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "space-location-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )


def _gps_payload(
    *,
    space_id: str,
    user_id: str,
    latitude: float,
    longitude: float,
    accuracy_m: float | None,
    updated_at: str | None,
) -> dict:
    """Shape used by both the local WS frame and the sealed
    federation event. **No `zone_name`, no `state`** — HA zones never
    reach a space-bound channel (§23.8.6).
    """
    return {
        "space_id": space_id,
        "user_id": user_id,
        "lat": latitude,
        "lon": longitude,
        "accuracy_m": accuracy_m,
        "updated_at": updated_at,
    }

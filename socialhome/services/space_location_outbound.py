"""Per-space location fan-out (§23.8.6).

Subscribes to :class:`PresenceUpdated` and, for every space where the
user has opted in (``location_share_enabled = 1``) AND the space has
the map feature on (``feature_location = 1``), publishes a payload
to space members on this instance plus every remote member instance.

Two privacy tiers, picked per-space via ``features.location_mode``:

* ``"gps"`` — payload is ``{mode, space_id, user_id, lat, lon,
  accuracy_m, updated_at}``. Default; preserves the current behaviour.
* ``"zone_only"`` — the originating instance matches the member's
  GPS to a space-defined zone (§23.8.7) using haversine + radius.
  If a zone matches, the payload is ``{mode, space_id, user_id,
  zone_id, zone_name, updated_at}`` — **no** raw coordinates leave
  the originating household. If no zone matches the update is
  silently skipped (no presence-without-zone leak).

Either way HA-defined zone names never reach a space-bound payload —
``PresenceUpdated.zone_name`` is household-only data (§23.8.5).

This is the space twin of the household ``presence.updated`` frame
emitted by :class:`RealtimeService`. Two different ``type`` strings,
two different consumers, one PresenceUpdated event.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import PresenceUpdated, SpaceLocationModeChanged
from ..domain.federation import FederationEventType
from ..domain.space import Space, SpaceZone
from ..infrastructure.event_bus import EventBus
from ..infrastructure.ws_manager import WebSocketManager
from ..repositories.presence_repo import AbstractPresenceRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.space_zone_repo import AbstractSpaceZoneRepo
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)

#: Earth's mean radius in metres — used by the haversine zone matcher.
_EARTH_RADIUS_M = 6_371_000.0


class SpaceLocationOutbound:
    """Fan a household :class:`PresenceUpdated` out to every opted-in
    space, both locally (WS) and to remote member instances
    (federation).
    """

    __slots__ = (
        "_bus",
        "_ws",
        "_federation",
        "_spaces",
        "_zones",
        "_users",
        "_presence",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        ws: WebSocketManager,
        federation_service: "FederationService",
        space_repo: AbstractSpaceRepo,
        space_zone_repo: AbstractSpaceZoneRepo,
        user_repo: AbstractUserRepo,
        presence_repo: AbstractPresenceRepo,
    ) -> None:
        self._bus = bus
        self._ws = ws
        self._federation = federation_service
        self._spaces = space_repo
        self._zones = space_zone_repo
        self._users = user_repo
        self._presence = presence_repo

    def wire(self) -> None:
        self._bus.subscribe(PresenceUpdated, self._on_presence_updated)
        self._bus.subscribe(
            SpaceLocationModeChanged,
            self._on_mode_changed,
        )

    async def _on_presence_updated(self, event: PresenceUpdated) -> None:
        # Skip when the accuracy gate dropped coordinates — there is
        # nothing useful to put on a space channel. Members without
        # coordinates remain visible on the household dashboard via
        # ``presence.updated`` (zone-name only).
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
            payload = await self._payload_for_space(
                space=space,
                user_id=user.user_id,
                latitude=event.latitude,
                longitude=event.longitude,
                accuracy_m=event.gps_accuracy_m,
                updated_at=event.updated_at,
            )
            if payload is None:
                # zone_only mode + outside every zone → silent skip.
                continue
            await self._broadcast_local(space.id, payload)
            await self._fan_out_federation(space.id, payload)

    async def _on_mode_changed(self, event: SpaceLocationModeChanged) -> None:
        """Refire latest presence for every opted-in member of *this
        one space* so receivers reflect the new privacy tier within
        seconds (§23.8.6 mode switch).

        Bounded to the affected space — no cross-space fan-out, no
        re-fetch of household-wide presence beyond a single
        ``list_active`` call.
        """
        space = await self._spaces.get(event.space_id)
        if space is None or not space.features.location:
            return
        try:
            members = await self._spaces.list_members(event.space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("space-location-outbound: list members failed: %s", exc)
            return
        opted_in = {m.user_id for m in members if m.location_share_enabled}
        if not opted_in:
            return
        try:
            presences = await self._presence.list_active()
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "space-location-outbound: list_active failed: %s",
                exc,
            )
            return
        now = datetime.now(timezone.utc).isoformat()
        for p in presences:
            if p.user_id not in opted_in:
                continue
            if p.latitude is None or p.longitude is None:
                continue
            payload = await self._payload_for_space(
                space=space,
                user_id=p.user_id,
                latitude=p.latitude,
                longitude=p.longitude,
                accuracy_m=p.gps_accuracy_m,
                updated_at=now,
            )
            if payload is None:
                continue
            await self._broadcast_local(space.id, payload)
            await self._fan_out_federation(space.id, payload)

    async def _payload_for_space(
        self,
        *,
        space: Space,
        user_id: str,
        latitude: float,
        longitude: float,
        accuracy_m: float | None,
        updated_at: str | None,
    ) -> dict | None:
        """Build the per-space payload, picking the shape from the
        space's privacy tier. Returns ``None`` to mean "skip this
        update for this space" (zone_only with no zone match)."""
        mode = space.features.location_mode
        if mode == "zone_only":
            zone = await self._match_zone(space.id, latitude, longitude)
            if zone is None:
                return None
            return {
                "mode": "zone_only",
                "space_id": space.id,
                "user_id": user_id,
                "zone_id": zone.id,
                "zone_name": zone.name,
                "updated_at": updated_at,
            }
        return {
            "mode": "gps",
            "space_id": space.id,
            "user_id": user_id,
            "lat": latitude,
            "lon": longitude,
            "accuracy_m": accuracy_m,
            "updated_at": updated_at,
        }

    async def _match_zone(
        self, space_id: str, latitude: float, longitude: float
    ) -> SpaceZone | None:
        """Return the closest space zone whose great-circle distance
        to ``(latitude, longitude)`` is within its ``radius_m``, or
        ``None`` if the point is outside every zone.
        """
        try:
            zones = await self._zones.list_for_space(space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "space-location-outbound: list zones failed for %s: %s",
                space_id,
                exc,
            )
            return None
        best: tuple[float, SpaceZone] | None = None
        for z in zones:
            d = _haversine_m(latitude, longitude, z.latitude, z.longitude)
            if d <= z.radius_m and (best is None or d < best[0]):
                best = (d, z)
        return best[1] if best is not None else None

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


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two ``(lat, lon)``
    pairs. Mirrors the client-side ``matchZoneName`` helper in
    ``client/src/components/SpaceLocationCard.tsx`` so origin-side
    matching agrees with what the receiving browser would compute on
    GPS payloads.
    """
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))

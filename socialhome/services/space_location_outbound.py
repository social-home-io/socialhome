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

A remote member's pin (inbound ``SPACE_LOCATION_UPDATED``, surfaced as
:class:`RemoteSpaceLocationUpdated`) gets the same local frame — never
the federation fan-out, since it came from the mesh.

This is the space twin of the household ``presence.updated`` frame
emitted by :class:`RealtimeService`. Two different ``type`` strings,
two different consumers, one PresenceUpdated event.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..domain.events import (
    PresenceUpdated,
    RemoteSpaceLocationUpdated,
    SpaceLocationModeChanged,
    SpaceMemberLocationOptedIn,
)
from ..domain.federation import FederationEventType
from ..domain.space import WRITER_ROLES, Space, SpaceZone
from ..domain.zone_geo import match_zone
from ..infrastructure.event_bus import EventBus
from ..infrastructure.ws_manager import WebSocketManager
from ..repositories.presence_repo import AbstractPresenceRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.space_zone_repo import AbstractSpaceZoneRepo
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from .space_writer_households import SpaceWriterHouseholds

log = logging.getLogger(__name__)


def _now_iso() -> str:
    """The current UTC time, ISO 8601 (one seam for tests)."""
    return datetime.now(UTC).isoformat()


def _before(stamp: object, other: str) -> bool:
    """``stamp`` is an earlier UTC time than ``other`` (unreadable: yes)."""
    try:
        a = datetime.fromisoformat(str(stamp))
        b = datetime.fromisoformat(other)
    except ValueError:
        return True
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    if b.tzinfo is None:
        b = b.replace(tzinfo=UTC)
    return a < b


class SpaceLocationOutbound:
    """Fan a household :class:`PresenceUpdated` out to every opted-in
    space, both locally (WS) and to remote member instances
    (federation).
    """

    __slots__ = (
        "_bus",
        "_federation",
        "_last_sent",
        "_members",
        "_presence",
        "_shared_from",
        "_spaces",
        "_users",
        "_ws",
        "_zones",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        ws: WebSocketManager,
        federation_service: FederationService,
        space_repo: AbstractSpaceRepo,
        space_zone_repo: AbstractSpaceZoneRepo,
        user_repo: AbstractUserRepo,
        presence_repo: AbstractPresenceRepo,
        member_households: SpaceWriterHouseholds,
    ) -> None:
        self._bus = bus
        #: A member's pin goes to member households only — never to one
        #: that only follows the space (owner decision 13).
        self._members = member_households
        self._ws = ws
        self._federation = federation_service
        self._spaces = space_repo
        self._zones = space_zone_repo
        self._users = user_repo
        self._presence = presence_repo
        #: (space, user) → (what the last pin said, the time it has said it
        #: since). A pin goes to member households only when what it says
        #: changes (owner decision 2026-10-22): peers show "here since …".
        #: In memory: a restart re-sends each pin once. Bounded by the
        #: members sharing a location.
        self._last_sent: dict[tuple[str, str], tuple[tuple, str | None]] = {}
        #: (space, user) → when they turned sharing on in this process, or
        #: — not seen (sharing began before a restart) — when this process
        #: first sent their pin. A pin never says "here since" earlier than
        #: that: where they were before they chose to share is not shared.
        self._shared_from: dict[tuple[str, str], str] = {}

    def wire(self) -> None:
        self._bus.subscribe(PresenceUpdated, self._on_presence_updated)
        self._bus.subscribe(
            SpaceLocationModeChanged,
            self._on_mode_changed,
        )
        self._bus.subscribe(
            SpaceMemberLocationOptedIn,
            self._on_member_opted_in,
        )
        self._bus.subscribe(
            RemoteSpaceLocationUpdated,
            self._on_remote_location,
        )

    async def _on_remote_location(self, event: RemoteSpaceLocationUpdated) -> None:
        """A remote member's pin was stored — emit the same local
        ``space_location_updated`` frame a local move produces, so the
        Map tab moves it live instead of on its 30 s poll.

        Same audience as a local pin (this space's local members) and the
        same tier rule as ``GET /api/spaces/{id}/presence``: *this*
        household's ``location_mode`` is the authority, so a pin whose
        mode disagrees (a peer still on GPS after a switch to zone_only)
        is not shown and never leaks coordinates. Local only — the pin
        came from the mesh, so it is not re-federated.
        """
        space = await self._spaces.get(event.space_id)
        if space is None or not space.features.location:
            return
        if event.mode != space.features.location_mode:
            return
        payload: dict
        if event.mode == "zone_only":
            payload = {
                "mode": "zone_only",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "zone_id": event.zone_id,
                "zone_name": event.zone_name,
                "updated_at": event.updated_at,
            }
        else:
            if event.latitude is None or event.longitude is None:
                return
            payload = {
                "mode": "gps",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "lat": round(float(event.latitude), 4),
                "lon": round(float(event.longitude), 4),
                "accuracy_m": event.accuracy_m,
                "updated_at": event.updated_at,
            }
        await self._broadcast_local(event.space_id, payload)

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
                updated_at=event.located_since or event.updated_at,
            )
            if payload is None:
                # zone_only mode + outside every zone → silent skip.
                continue
            await self._publish(space.id, payload)

    async def _on_member_opted_in(
        self,
        event: SpaceMemberLocationOptedIn,
    ) -> None:
        """Refire the member's current presence for this one space on
        opt-in, so a fresh "Share my location" tap produces an
        immediate pin instead of waiting for the next HA push (could
        be minutes).

        Bounded to one (space, user) pair — no cross-space fan-out.
        Silent skip if the space lacks ``feature_location``, the
        member's local presence row has no GPS yet, or zone_only +
        outside every zone.
        """
        space = await self._spaces.get(event.space_id)
        if space is None or not space.features.location:
            return
        try:
            presences = await self._presence.list_active()
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "space-location-outbound: list_active failed: %s",
                exc,
                exc_info=True,
            )
            return
        match = next(
            (p for p in presences if p.user_id == event.user_id),
            None,
        )
        if match is None or match.latitude is None or match.longitude is None:
            # No active GPS to share yet — the next ``PresenceUpdated``
            # via HA will be the one that produces the pin.
            return
        payload = await self._payload_for_space(
            space=space,
            user_id=event.user_id,
            latitude=match.latitude,
            longitude=match.longitude,
            accuracy_m=match.gps_accuracy_m,
            updated_at=(
                match.located_since or match.updated_at or datetime.now(UTC).isoformat()
            ),
        )
        if payload is None:
            return
        self._shared_from[(space.id, event.user_id)] = _now_iso()
        await self._publish(space.id, payload, force=True)

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
            log.debug(
                "space-location-outbound: list members failed: %s", exc, exc_info=True
            )
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
                exc_info=True,
            )
            return
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
                updated_at=(
                    p.located_since or p.updated_at or datetime.now(UTC).isoformat()
                ),
            )
            if payload is None:
                continue
            await self._publish(space.id, payload, force=True)

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
                exc_info=True,
            )
            return None
        return match_zone(zones, latitude, longitude)

    # ── Fan-out helpers ────────────────────────────────────────────────

    async def _broadcast_local(self, space_id: str, payload: dict) -> None:
        """The pin to this household's members of the space — never its
        followers, who may not read the map (owner decision 13)."""
        try:
            local = await self._spaces.list_local_member_user_ids(space_id)
            members = {
                m.user_id
                for m in await self._spaces.list_members(space_id)
                if m.role in WRITER_ROLES
            }
            user_ids = [uid for uid in local if uid in members]
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "space-location-outbound: list local members failed: %s",
                exc,
                exc_info=True,
            )
            return
        await self._ws.broadcast_to_users(
            user_ids,
            {"type": "space_location_updated", "data": payload},
        )

    async def _publish(
        self, space_id: str, payload: dict, *, force: bool = False
    ) -> None:
        """Show the pin to this household's members and send it to the
        space's member households — the latter only when what it says
        changed (or ``force``: an opt-in, a tier switch, which exist to put
        the pin on the map now).

        ``updated_at`` is the time the pin has said this since — on the
        wire and in the local frame alike, so every map reads "here since
        …". In ``zone_only`` that is when the person entered the zone: a
        move inside it keeps the time the zone was first sent."""
        key = (space_id, str(payload.get("user_id") or ""))
        said = (
            (payload.get("mode"), payload.get("zone_id"))
            if payload.get("mode") == "zone_only"
            else (payload.get("mode"), payload.get("lat"), payload.get("lon"))
        )
        last = self._last_sent.get(key)
        unchanged = last is not None and last[0] == said
        if unchanged and last is not None and last[1]:
            payload = {**payload, "updated_at": last[1]}
        # The opt-in time when we saw it; otherwise (sharing began before
        # this process — a restart) the time this process first sends the
        # pin. Either way never earlier: a slightly late "since" is fine,
        # where they were before they chose to share is not.
        start = self._shared_from.setdefault(key, _now_iso())
        if _before(payload.get("updated_at"), start):
            payload = {**payload, "updated_at": start}
        self._last_sent[key] = (said, payload.get("updated_at"))
        await self._broadcast_local(space_id, payload)
        if unchanged and not force:
            return
        await self._fan_out_federation(space_id, payload)

    async def _fan_out_federation(
        self,
        space_id: str,
        payload: dict,
    ) -> None:
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                FederationEventType.SPACE_LOCATION_UPDATED,
                payload,
                only_instances=await self._members.writer_households(space_id),
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "space-location-outbound: broadcast failed for space=%s: %s",
                space_id,
                exc,
                exc_info=True,
            )

"""Space-zone service — admin-gated CRUD for the per-space zone catalogue (§23.8.7).

Each space owns a small catalogue of named display circles. Members' GPS
positions are matched to zones client-side; the server never stores the
match or sends "member X is in zone Y" preprocessed labels. Zones are
display data — they never replace coordinates on a space-bound payload.

Service responsibilities:

* admin gate — only ``owner`` or ``admin`` members of the space may
  create / update / delete; any member may list. Mirrors the pattern in
  :mod:`space_service`.
* validation — radius range (25 m – 50 km), color hex shape, name
  uniqueness within a space, 50-zones-per-space cap.
* GPS truncation — every persisted coordinate goes through
  :func:`truncate_coord` (§25 / CLAUDE.md).
* domain events — emit :class:`SpaceZoneUpserted` /
  :class:`SpaceZoneDeleted` after a successful write so federation +
  realtime fan-outs can pick them up via the bus.

This module never talks to SQLite directly; it works against the
:class:`AbstractSpaceZoneRepo` and :class:`AbstractSpaceRepo` protocols
plus an :class:`EventBus` for fan-out.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from ..domain.events import SpaceZoneDeleted, SpaceZoneUpserted
from ..domain.presence import truncate_coord
from ..domain.space import SpacePermissionError, SpaceZone
from ..infrastructure.event_bus import EventBus
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.space_zone_repo import AbstractSpaceZoneRepo
from ..repositories.user_repo import AbstractUserRepo

#: §23.8.7: 25 m floor (just above the 4-dp ~11 m precision); 50 km
#: ceiling (a "city-wide" zone is the largest meaningful display
#: bucket on a per-space map).
MIN_RADIUS_M = 25
MAX_RADIUS_M = 50_000

#: §23.8.7: cap enforced at the service layer so we can adjust without
#: a schema migration. With this cap the client-side zone-match per
#: pin is trivially fast.
MAX_ZONES_PER_SPACE = 50

#: ``"#RRGGBB"`` (case-insensitive) or ``None``. Matches the spec's
#: documented shape; the client picks a deterministic palette colour
#: when a zone has ``color is None``.
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

#: Sentinel for "argument omitted" so PATCH callers can distinguish
#: "don't change colour" (omit) from "clear the colour, fall back to
#: palette default" (pass ``None`` explicitly).
_UNSET: object = object()


class SpaceZoneNotFoundError(Exception):
    """Raised when a referenced zone id does not exist (or is in
    another space than the one in the URL).
    """


class SpaceZoneLimitError(Exception):
    """Raised when a space already has :data:`MAX_ZONES_PER_SPACE`
    zones and a new one cannot be created.
    """


class SpaceZoneNameConflictError(Exception):
    """Raised when a zone name collides with an existing zone in the
    same space (DB ``UNIQUE (space_id, name)``).
    """


class SpaceZoneService:
    """Admin-gated CRUD for per-space zones (§23.8.7)."""

    __slots__ = ("_zones", "_spaces", "_users", "_bus")

    def __init__(
        self,
        zone_repo: AbstractSpaceZoneRepo,
        space_repo: AbstractSpaceRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._zones = zone_repo
        self._spaces = space_repo
        self._users = user_repo
        self._bus = bus

    def attach_event_bus(self, bus: EventBus) -> None:
        """Attach an :class:`EventBus` after construction (mirrors
        :meth:`PresenceService.attach_event_bus`)."""
        self._bus = bus

    # ── Read ────────────────────────────────────────────────────────────

    async def list_zones(
        self,
        space_id: str,
        actor_user_id: str,
    ) -> list[SpaceZone]:
        """List zones for ``space_id``. Caller must be a space member."""
        await self._require_member(space_id, actor_user_id)
        return await self._zones.list_for_space(space_id)

    # ── Write ───────────────────────────────────────────────────────────

    async def create_zone(
        self,
        space_id: str,
        actor_username: str,
        *,
        name: str,
        latitude: float,
        longitude: float,
        radius_m: int,
        color: str | None = None,
    ) -> SpaceZone:
        """Create a new zone. Admin/owner-only."""
        await self._require_admin(space_id, actor_username)
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        clean_name = _validate_name(name)
        clean_radius = _validate_radius(radius_m)
        clean_color = _validate_color(color)
        clean_lat = truncate_coord(float(latitude))
        clean_lon = truncate_coord(float(longitude))
        assert clean_lat is not None and clean_lon is not None  # noqa: S101

        existing = await self._zones.count_for_space(space_id)
        if existing >= MAX_ZONES_PER_SPACE:
            raise SpaceZoneLimitError(
                f"space already has {MAX_ZONES_PER_SPACE} zones",
            )
        if await self._zones.get_by_name(space_id, clean_name) is not None:
            raise SpaceZoneNameConflictError(
                f"a zone called {clean_name!r} already exists in this space",
            )

        now = _now_iso()
        zone = SpaceZone(
            id=f"z_{secrets.token_urlsafe(12)}",
            space_id=space_id,
            name=clean_name,
            latitude=clean_lat,
            longitude=clean_lon,
            radius_m=clean_radius,
            color=clean_color,
            created_by=actor.user_id,
            created_at=now,
            updated_at=now,
        )
        await self._zones.upsert(zone)
        await self._publish_upserted(zone)
        return zone

    async def update_zone(
        self,
        space_id: str,
        zone_id: str,
        actor_username: str,
        *,
        name: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_m: int | None = None,
        color: "str | None | object" = _UNSET,
    ) -> SpaceZone:
        """Partial update. Admin/owner-only.

        ``color`` accepts three states: omit (no change), explicit
        ``None`` (clear the colour, fall back to palette default), or
        a hex string. We use a sentinel so ``color=None`` actually
        means "clear it" rather than "do nothing".
        """
        await self._require_admin(space_id, actor_username)
        existing = await self._zones.get(zone_id)
        if existing is None or existing.space_id != space_id:
            raise SpaceZoneNotFoundError(zone_id)

        new_name = existing.name if name is None else _validate_name(name)
        new_radius = (
            existing.radius_m if radius_m is None else _validate_radius(radius_m)
        )
        new_color = (
            existing.color if color is _UNSET else _validate_color(color)  # type: ignore[arg-type]
        )
        new_lat = (
            existing.latitude if latitude is None else truncate_coord(float(latitude))
        )
        new_lon = (
            existing.longitude
            if longitude is None
            else truncate_coord(float(longitude))
        )
        assert new_lat is not None and new_lon is not None  # noqa: S101

        if new_name != existing.name:
            clash = await self._zones.get_by_name(space_id, new_name)
            if clash is not None and clash.id != existing.id:
                raise SpaceZoneNameConflictError(
                    f"a zone called {new_name!r} already exists in this space",
                )

        updated = SpaceZone(
            id=existing.id,
            space_id=existing.space_id,
            name=new_name,
            latitude=new_lat,
            longitude=new_lon,
            radius_m=new_radius,
            color=new_color,
            created_by=existing.created_by,
            created_at=existing.created_at,
            updated_at=_now_iso(),
        )
        await self._zones.upsert(updated)
        await self._publish_upserted(updated)
        return updated

    async def delete_zone(
        self,
        space_id: str,
        zone_id: str,
        actor_username: str,
    ) -> None:
        """Delete a zone. Admin/owner-only."""
        await self._require_admin(space_id, actor_username)
        existing = await self._zones.get(zone_id)
        if existing is None or existing.space_id != space_id:
            raise SpaceZoneNotFoundError(zone_id)
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        await self._zones.delete(zone_id)
        if self._bus is not None:
            await self._bus.publish(
                SpaceZoneDeleted(
                    space_id=space_id,
                    zone_id=zone_id,
                    deleted_by=actor.user_id,
                ),
            )

    # ── Internal helpers ────────────────────────────────────────────────

    async def _require_member(
        self,
        space_id: str,
        actor_user_id: str,
    ) -> None:
        member = await self._spaces.get_member(space_id, actor_user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")

    async def _require_admin(
        self,
        space_id: str,
        actor_username: str,
    ) -> None:
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        member = await self._spaces.get_member(space_id, actor.user_id)
        if member is None or member.role not in ("owner", "admin"):
            raise SpacePermissionError("admin or owner required")

    async def _publish_upserted(self, zone: SpaceZone) -> None:
        if self._bus is None:
            return
        await self._bus.publish(
            SpaceZoneUpserted(
                space_id=zone.space_id,
                zone_id=zone.id,
                name=zone.name,
                latitude=zone.latitude,
                longitude=zone.longitude,
                radius_m=zone.radius_m,
                color=zone.color,
                created_by=zone.created_by,
                updated_at=zone.updated_at,
            ),
        )


# ─── Validation helpers ───────────────────────────────────────────────────


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("zone name must not be empty")
    if len(cleaned) > 64:
        raise ValueError("zone name must be 64 characters or fewer")
    return cleaned


def _validate_radius(radius_m: int) -> int:
    try:
        coerced = int(radius_m)
    except (TypeError, ValueError) as exc:
        raise ValueError("radius_m must be an integer") from exc
    if not (MIN_RADIUS_M <= coerced <= MAX_RADIUS_M):
        raise ValueError(
            f"radius_m must be between {MIN_RADIUS_M} and {MAX_RADIUS_M}",
        )
    return coerced


def _validate_color(color: str | None) -> str | None:
    if color is None:
        return None
    if not isinstance(color, str) or not _COLOR_RE.match(color):
        raise ValueError("color must be a #RRGGBB hex string or None")
    return color.lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

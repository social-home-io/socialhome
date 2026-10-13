"""Household-zone service — the household's own named places.

Household zones are labelled circles (Home, School, Work…) that belong to
the household and are **never federated** — the zone names stay local
(§23.8.6: space-bound presence strips ``zone_name``). Two writers:

* household admins, through ``/api/household/zones`` — zones created in
  Social Home (``external_id`` NULL);
* a platform sync (Home Assistant's zones, a later change) through
  :meth:`HouseholdZoneService.mirror_external` — those rows carry the
  platform's id in ``external_id`` and are read-only through the API.

Rules:

* any household member may list; only household admins write
  (:class:`PermissionError` → 403);
* field validation reuses the space-zone validators (name, 4-dp centre,
  25 m – 50 km radius, ``#RRGGBB`` colour, one-emoji icon);
* names are unique ignoring case; at most :data:`MAX_HOUSEHOLD_ZONES`;
* at most one Home zone. Marking a zone as Home moves the flag; the Home
  zone can't be deleted or un-marked (mark another zone instead), and a
  synced Home (Home Assistant's ``zone.home``) is never taken over by a
  zone created here — the synced one is the source of truth;
* every change publishes :class:`HouseholdZonesChanged` (a refetch nudge).

:meth:`match` is the server-side GPS→zone match
(:func:`socialhome.domain.zone_geo.match_zone`) that
:class:`PresenceService` uses when a location update carries no
``zone_name``. It reads the ≤ 50 rows per call — cheap, and never stale
(a backup restore writes the table behind the service's back).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone

from ..domain.events import HouseholdZonesChanged
from ..domain.household_zone import (
    MAX_HOUSEHOLD_ZONES,
    ExternalHouseholdZone,
    HouseholdZone,
    HouseholdZoneIsHomeError,
    HouseholdZoneLimitError,
    HouseholdZoneMirrorResult,
    HouseholdZoneNameTakenError,
    HouseholdZoneNotFoundError,
    HouseholdZoneReadOnlyError,
)
from ..domain.space import (
    MAX_ZONE_NAME_LENGTH,
    validate_zone_color,
    validate_zone_coord,
    validate_zone_icon,
    validate_zone_name,
    validate_zone_radius,
)
from ..domain.zone_geo import match_zone
from ..infrastructure.event_bus import EventBus
from ..repositories.household_zone_repo import AbstractHouseholdZoneRepo
from .bus_publisher import BusPublisherMixin

__all__ = ["HouseholdZoneService"]

log = logging.getLogger(__name__)

#: Longest platform id we keep (HA entity ids are far shorter).
MAX_EXTERNAL_ID_LENGTH = 255

#: Sentinel: PATCH field omitted (vs. an explicit ``None`` = clear).
_UNSET: object = object()

_HOME_MANAGED = "The Home zone is managed by Home Assistant."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_admin(actor_is_admin: bool) -> None:
    if not actor_is_admin:
        raise PermissionError("Only household admins can change household zones.")


def _validate_is_home(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("is_home must be true or false")
    return value


def _same_place(a: HouseholdZone, b: HouseholdZone) -> bool:
    """Equal in every field a sync can change."""
    return (
        a.name,
        a.latitude,
        a.longitude,
        a.radius_m,
        a.color,
        a.icon,
        a.is_home,
    ) == (
        b.name,
        b.latitude,
        b.longitude,
        b.radius_m,
        b.color,
        b.icon,
        b.is_home,
    )


def _free_name(name: str, taken: set[str]) -> str:
    """``name``, or the first ``"name (n)"`` not in ``taken`` (lower-cased),
    kept within :data:`MAX_ZONE_NAME_LENGTH`."""
    if name.lower() not in taken:
        return name
    n = 2
    while True:
        suffix = f" ({n})"
        candidate = name[: MAX_ZONE_NAME_LENGTH - len(suffix)].rstrip() + suffix
        if candidate.lower() not in taken:
            return candidate
        n += 1


class HouseholdZoneService(BusPublisherMixin):
    """CRUD + platform mirror + GPS match for household zones."""

    __slots__ = ("_zones", "_bus")

    def __init__(
        self,
        zone_repo: AbstractHouseholdZoneRepo,
        *,
        bus: EventBus | None = None,
    ) -> None:
        self._zones = zone_repo
        self._bus = bus

    # ── Read ────────────────────────────────────────────────────────────

    async def list_zones(self) -> list[HouseholdZone]:
        """Every zone — Home first, then by name."""
        return await self._zones.list()

    async def count_zones(self) -> int:
        return await self._zones.count()

    async def match(self, latitude: float, longitude: float) -> HouseholdZone | None:
        """The zone ``(latitude, longitude)`` is in (nearest centre within
        its radius), or ``None``."""
        return match_zone(await self._zones.list(), latitude, longitude)

    # ── Write (household admins) ───────────────────────────────────────

    async def create_zone(
        self,
        *,
        actor_is_admin: bool,
        name: object,
        latitude: object,
        longitude: object,
        radius_m: object,
        color: object = None,
        icon: object = None,
        is_home: object = False,
    ) -> HouseholdZone:
        """Create a zone in Social Home (``external_id`` stays NULL)."""
        _require_admin(actor_is_admin)
        clean_name = validate_zone_name(name)
        clean_lat = validate_zone_coord(latitude, name="latitude", limit=90)
        clean_lon = validate_zone_coord(longitude, name="longitude", limit=180)
        clean_radius = validate_zone_radius(radius_m)
        clean_color = validate_zone_color(color)
        clean_icon = validate_zone_icon(icon)
        clean_home = _validate_is_home(is_home)

        if await self._zones.count() >= MAX_HOUSEHOLD_ZONES:
            raise HouseholdZoneLimitError()
        if await self._zones.get_by_name(clean_name) is not None:
            raise HouseholdZoneNameTakenError()
        if clean_home:
            await self._refuse_if_home_is_managed()

        now = _now_iso()
        zone = HouseholdZone(
            id=f"hz_{secrets.token_urlsafe(12)}",
            name=clean_name,
            latitude=clean_lat,
            longitude=clean_lon,
            radius_m=clean_radius,
            color=clean_color,
            icon=clean_icon,
            is_home=clean_home,
            external_id=None,
            created_at=now,
            updated_at=now,
        )
        await self._zones.create(zone)
        await self._emit(HouseholdZonesChanged())
        return zone

    async def update_zone(
        self,
        zone_id: str,
        *,
        actor_is_admin: bool,
        name: object = _UNSET,
        latitude: object = _UNSET,
        longitude: object = _UNSET,
        radius_m: object = _UNSET,
        color: object = _UNSET,
        icon: object = _UNSET,
        is_home: object = _UNSET,
    ) -> HouseholdZone:
        """Partial update. An omitted field stays; ``color`` / ``icon``
        accept an explicit ``None`` to clear."""
        _require_admin(actor_is_admin)
        existing = await self._zones.get(zone_id)
        if existing is None:
            raise HouseholdZoneNotFoundError()
        if existing.read_only:
            raise HouseholdZoneReadOnlyError()

        new_name = existing.name if name is _UNSET else validate_zone_name(name)
        new_lat = (
            existing.latitude
            if latitude is _UNSET
            else validate_zone_coord(latitude, name="latitude", limit=90)
        )
        new_lon = (
            existing.longitude
            if longitude is _UNSET
            else validate_zone_coord(longitude, name="longitude", limit=180)
        )
        new_radius = (
            existing.radius_m if radius_m is _UNSET else validate_zone_radius(radius_m)
        )
        new_color = existing.color if color is _UNSET else validate_zone_color(color)
        new_icon = existing.icon if icon is _UNSET else validate_zone_icon(icon)
        new_home = existing.is_home if is_home is _UNSET else _validate_is_home(is_home)

        if existing.is_home and not new_home:
            raise HouseholdZoneIsHomeError()
        if new_home and not existing.is_home:
            await self._refuse_if_home_is_managed()
        if new_name.lower() != existing.name.lower():
            clash = await self._zones.get_by_name(new_name)
            if clash is not None and clash.id != existing.id:
                raise HouseholdZoneNameTakenError()

        updated = HouseholdZone(
            id=existing.id,
            name=new_name,
            latitude=new_lat,
            longitude=new_lon,
            radius_m=new_radius,
            color=new_color,
            icon=new_icon,
            is_home=new_home,
            external_id=None,
            created_at=existing.created_at,
            updated_at=_now_iso(),
        )
        if not await self._zones.update(updated):
            raise HouseholdZoneNotFoundError()
        await self._emit(HouseholdZonesChanged())
        return updated

    async def delete_zone(self, zone_id: str, *, actor_is_admin: bool) -> None:
        """Delete a zone created here. The Home zone and synced zones stay."""
        _require_admin(actor_is_admin)
        existing = await self._zones.get(zone_id)
        if existing is None:
            raise HouseholdZoneNotFoundError()
        if existing.read_only:
            raise HouseholdZoneReadOnlyError()
        if existing.is_home:
            raise HouseholdZoneIsHomeError()
        if not await self._zones.delete(zone_id):
            raise HouseholdZoneNotFoundError()
        await self._emit(HouseholdZonesChanged())

    # ── Platform sync ──────────────────────────────────────────────────

    async def mirror_external(
        self, source: Sequence[ExternalHouseholdZone]
    ) -> HouseholdZoneMirrorResult:
        """Make the synced zones exactly ``source`` (keyed on
        ``external_id``) without touching zones created here.

        Invalid entries, repeated ``external_id`` values and a second Home
        are skipped (logged at WARNING). A synced name that collides with
        another zone gets a ``" (n)"`` suffix — zones created here keep
        theirs. Zones that would push the household past
        :data:`MAX_HOUSEHOLD_ZONES` are skipped, the Home zone first in
        line. A synced Home takes the Home flag. Nothing is written (and
        no event published) when the set is unchanged.
        """
        current = await self._zones.list()
        native = [z for z in current if not z.read_only]
        synced: dict[str | None, HouseholdZone] = {
            z.external_id: z for z in current if z.external_id is not None
        }

        valid: list[HouseholdZone] = []
        seen_ids: set[str] = set()
        have_home = False
        skipped = 0
        now = _now_iso()
        for ext in source:
            zone = self._external_to_zone(ext, now)
            if zone is None or zone.external_id in seen_ids:
                skipped += 1
                continue
            assert zone.external_id is not None  # noqa: S101 — set above
            seen_ids.add(zone.external_id)
            if zone.is_home and have_home:
                log.warning(
                    "household zones: second Home zone %s from the sync ignored",
                    zone.external_id,
                )
                zone = replace(zone, is_home=False)
            have_home = have_home or zone.is_home
            valid.append(zone)

        # Home first in line for the cap; otherwise the source order.
        valid.sort(key=lambda z: not z.is_home)
        room = max(0, MAX_HOUSEHOLD_ZONES - len(native))
        if len(valid) > room:
            log.warning(
                "household zones: %d synced zones over the %d-zone cap skipped",
                len(valid) - room,
                MAX_HOUSEHOLD_ZONES,
            )
            skipped += len(valid) - room
            valid = valid[:room]

        taken = {z.name.lower() for z in native}
        desired: list[HouseholdZone] = []
        for zone in valid:
            zone = replace(zone, name=_free_name(zone.name, taken))
            taken.add(zone.name.lower())
            old = synced.get(zone.external_id)
            if old is not None:
                # Keep the row's id and created_at; an unchanged zone also
                # keeps its updated_at.
                zone = replace(
                    zone,
                    id=old.id,
                    created_at=old.created_at,
                    updated_at=(
                        old.updated_at if _same_place(zone, old) else zone.updated_at
                    ),
                )
            desired.append(zone)

        native_home = any(z.is_home for z in native)
        unchanged = (
            set(synced) == {z.external_id for z in desired}
            and all(_same_place(z, synced[z.external_id]) for z in desired)
            and not (have_home and native_home)
        )
        if unchanged:
            return HouseholdZoneMirrorResult(
                upserted=0, removed=0, skipped=skipped, changed=False
            )

        upserted, removed = await self._zones.mirror_external(desired)
        await self._emit(HouseholdZonesChanged())
        return HouseholdZoneMirrorResult(
            upserted=upserted, removed=removed, skipped=skipped, changed=True
        )

    # ── Internal ───────────────────────────────────────────────────────

    async def _refuse_if_home_is_managed(self) -> None:
        current = next((z for z in await self._zones.list() if z.is_home), None)
        if current is not None and current.read_only:
            raise HouseholdZoneReadOnlyError(_HOME_MANAGED)

    @staticmethod
    def _external_to_zone(ext: ExternalHouseholdZone, now: str) -> HouseholdZone | None:
        """Validate one synced zone; ``None`` (logged) when it is invalid."""
        eid = ext.external_id
        if not isinstance(eid, str) or not eid or len(eid) > MAX_EXTERNAL_ID_LENGTH:
            log.warning("household zones: synced zone without a usable id skipped")
            return None
        try:
            return HouseholdZone(
                id=f"hz_{secrets.token_urlsafe(12)}",
                name=validate_zone_name(ext.name),
                latitude=validate_zone_coord(ext.latitude, name="latitude", limit=90),
                longitude=validate_zone_coord(
                    ext.longitude, name="longitude", limit=180
                ),
                radius_m=validate_zone_radius(ext.radius_m),
                color=validate_zone_color(ext.color),
                icon=validate_zone_icon(ext.icon),
                is_home=_validate_is_home(ext.is_home),
                external_id=eid,
                created_at=now,
                updated_at=now,
            )
        except ValueError as exc:
            log.warning("household zones: synced zone %s skipped: %s", eid, exc)
            return None

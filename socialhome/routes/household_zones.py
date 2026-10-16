"""Household-zone routes — ``/api/household/zones``.

Thin handlers over :class:`HouseholdZoneService`. Any household member
lists; household admins create / edit / delete. Every method needs the
household's presence feature on (``FEATURE_DISABLED`` otherwise), like
the presence routes. Domain errors map centrally in
:meth:`BaseView._iter`:

* :class:`PermissionError` (not an admin)          → 403 ``FORBIDDEN``
* ``HouseholdZoneNotFoundError``                   → 404 ``NOT_FOUND``
* ``HouseholdZoneLimitError``                      → 409 ``ZONE_LIMIT``
* ``HouseholdZoneNameTakenError``                  → 409 ``ZONE_NAME_TAKEN``
* ``HouseholdZoneReadOnlyError`` (synced zone)     → 409 ``ZONE_READ_ONLY``
* ``HouseholdZoneIsHomeError``                     → 409 ``ZONE_IS_HOME``
* ``ValueError`` (name / centre / radius / colour / icon / is_home) → 422

``/api/household/zones/sync`` reads and sets the platform zone sync
(:class:`HouseholdZoneSync` — Home Assistant's zones, on by default);
household admins only.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ..app_keys import household_zone_service_key, household_zone_sync_key
from ..domain.errors import WrongTypeError
from ..domain.household_zone import HouseholdZone, HouseholdZoneSyncStatus
from ..services.household_zone_service import HouseholdZoneService
from ..services.household_zone_sync import HouseholdZoneSync
from .base import BaseView

__all__ = [
    "HouseholdZoneDetailView",
    "HouseholdZoneSyncView",
    "HouseholdZonesCollectionView",
]

#: Body fields a PATCH may carry; an omitted one stays unchanged.
_PATCH_FIELDS = (
    "name",
    "latitude",
    "longitude",
    "radius_m",
    "color",
    "icon",
    "is_home",
)


def household_zone_to_dict(z: HouseholdZone) -> dict[str, Any]:
    """The API shape of one household zone."""
    return {
        "id": z.id,
        "name": z.name,
        "latitude": z.latitude,
        "longitude": z.longitude,
        "radius_m": z.radius_m,
        "color": z.color,
        "icon": z.icon,
        "is_home": z.is_home,
        "external_id": z.external_id,
        "read_only": z.read_only,
        "created_at": z.created_at,
        "updated_at": z.updated_at,
    }


def household_zone_sync_to_dict(s: HouseholdZoneSyncStatus) -> dict[str, Any]:
    """The API shape of the zone-sync status."""
    return {
        "available": s.available,
        "enabled": s.enabled,
        "last_synced_at": s.last_synced_at,
        "last_error": s.last_error,
    }


class _HouseholdZoneView(BaseView):
    @property
    def zones(self) -> HouseholdZoneService:
        svc: HouseholdZoneService = self.svc(household_zone_service_key)
        return svc

    @property
    def is_admin(self) -> bool:
        return bool(self.user.is_admin)

    async def _gate(self) -> None:
        # The auth middleware already answered 401 for an anonymous call.
        await self.require_household_feature("presence")

    async def _object_body(self) -> dict[str, Any]:
        body = await self.body()
        if not isinstance(body, dict):
            raise WrongTypeError("body must be a JSON object")
        return body


class HouseholdZonesCollectionView(_HouseholdZoneView):
    """``GET`` / ``POST /api/household/zones``."""

    async def get(self) -> web.Response:
        await self._gate()
        zones = await self.zones.list_zones()
        return self._json({"zones": [household_zone_to_dict(z) for z in zones]})

    async def post(self) -> web.Response:
        await self._gate()
        body = await self._object_body()
        # Raw values: the domain validators coerce and refuse (bools,
        # NaN / inf, out of range) — a 422, never a 500.
        zone = await self.zones.create_zone(
            actor_is_admin=self.is_admin,
            name=body.get("name", ""),
            latitude=body.get("latitude"),
            longitude=body.get("longitude"),
            radius_m=body.get("radius_m"),
            color=body.get("color"),
            icon=body.get("icon"),
            is_home=body.get("is_home", False),
        )
        return self._json(household_zone_to_dict(zone), status=201)


class HouseholdZoneDetailView(_HouseholdZoneView):
    """``PATCH`` / ``DELETE /api/household/zones/{zone_id}``.

    PATCH is partial: omit a field to leave it; ``"color": null`` /
    ``"icon": null`` clear them.
    """

    async def patch(self) -> web.Response:
        await self._gate()
        body = await self._object_body()
        changes = {k: body[k] for k in _PATCH_FIELDS if k in body}
        zone = await self.zones.update_zone(
            self.match("zone_id"), actor_is_admin=self.is_admin, **changes
        )
        return self._json(household_zone_to_dict(zone))

    async def delete(self) -> web.Response:
        await self._gate()
        await self.zones.delete_zone(
            self.match("zone_id"), actor_is_admin=self.is_admin
        )
        return web.Response(status=204)


class HouseholdZoneSyncView(_HouseholdZoneView):
    """``GET`` / ``PATCH /api/household/zones/sync`` — household admins.

    GET answers ``{available, enabled, last_synced_at, last_error}``;
    PATCH ``{"enabled": bool}`` stores the option, syncs right away (on:
    pull the platform's zones; off: remove the synced ones) and answers
    the new status. Without a platform zone source PATCH is a 422.
    """

    @property
    def sync(self) -> HouseholdZoneSync:
        svc: HouseholdZoneSync = self.svc(household_zone_sync_key)
        return svc

    def _require_admin(self) -> None:
        if not self.is_admin:
            raise PermissionError("Only household admins can change zone sync.")

    async def get(self) -> web.Response:
        await self._gate()
        self._require_admin()
        return self._json(household_zone_sync_to_dict(await self.sync.status()))

    async def patch(self) -> web.Response:
        await self._gate()
        self._require_admin()
        body = await self._object_body()
        status = await self.sync.set_enabled(body.get("enabled"))
        return self._json(household_zone_sync_to_dict(status))

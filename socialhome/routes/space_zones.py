"""Space-zone routes — ``/api/spaces/{id}/zones`` (§23.8.7).

Thin handlers delegating to :class:`SpaceZoneService`. Domain
exceptions are mapped centrally by :class:`BaseView._iter`:

* :class:`SpaceZoneNotFoundError`           → 404 ``NOT_FOUND``
* :class:`SpaceZoneLimitError`              → 409 ``ZONE_LIMIT``
* :class:`SpaceZoneNameConflictError`       → 409 ``ZONE_NAME_TAKEN``
* :class:`SpacePermissionError`             → 403 ``FORBIDDEN``
* ``ValueError`` (radius / colour / name)   → 422 ``UNPROCESSABLE``
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import space_zone_service_key
from ..domain.space import SpaceZone
from ..security import error_response
from .base import BaseView


def _zone_to_dict(z: SpaceZone) -> dict:
    return {
        "id": z.id,
        "space_id": z.space_id,
        "name": z.name,
        "latitude": z.latitude,
        "longitude": z.longitude,
        "radius_m": z.radius_m,
        "color": z.color,
        "created_by": z.created_by,
        "created_at": z.created_at,
        "updated_at": z.updated_at,
    }


def _ensure_authenticated(view: "BaseView") -> web.Response | None:
    ctx = view.user
    if ctx is None or ctx.user_id is None:
        return error_response(
            401,
            "UNAUTHENTICATED",
            "Authentication required.",
        )
    return None


class SpaceZonesCollectionView(BaseView):
    """``GET`` and ``POST`` ``/api/spaces/{id}/zones``.

    * ``GET`` is open to any space member.
    * ``POST`` is admin/owner-only — the central error mapper turns
      :class:`SpacePermissionError` into a 403 envelope.
    """

    async def get(self) -> web.Response:
        unauth = _ensure_authenticated(self)
        if unauth is not None:
            return unauth
        space_id = self.match("id")
        svc = self.svc(space_zone_service_key)
        zones = await svc.list_zones(space_id, self.user.user_id)
        return self._json({"zones": [_zone_to_dict(z) for z in zones]})

    async def post(self) -> web.Response:
        unauth = _ensure_authenticated(self)
        if unauth is not None:
            return unauth
        space_id = self.match("id")
        body = await self.body()
        svc = self.svc(space_zone_service_key)
        zone = await svc.create_zone(
            space_id,
            self.user.username,
            name=str(body.get("name", "")),
            latitude=float(body.get("latitude", 0.0)),
            longitude=float(body.get("longitude", 0.0)),
            radius_m=int(body.get("radius_m", 0)),
            color=body.get("color"),
        )
        return self._json(_zone_to_dict(zone), status=201)


class SpaceZoneDetailView(BaseView):
    """``PATCH`` and ``DELETE`` ``/api/spaces/{id}/zones/{zone_id}``.

    Both are admin/owner-only. Partial updates: omit any of
    ``name``, ``latitude``, ``longitude``, ``radius_m`` to leave them
    untouched. Send ``"color": null`` to clear the colour explicitly,
    omit the field to leave it untouched.
    """

    async def patch(self) -> web.Response:
        unauth = _ensure_authenticated(self)
        if unauth is not None:
            return unauth
        space_id = self.match("id")
        zone_id = self.match("zone_id")
        body = await self.body()
        svc = self.svc(space_zone_service_key)

        kwargs: dict = {}
        if "name" in body:
            kwargs["name"] = str(body["name"])
        if "latitude" in body:
            kwargs["latitude"] = float(body["latitude"])
        if "longitude" in body:
            kwargs["longitude"] = float(body["longitude"])
        if "radius_m" in body:
            kwargs["radius_m"] = int(body["radius_m"])
        if "color" in body:  # explicit None means "clear"
            kwargs["color"] = body["color"]

        zone = await svc.update_zone(
            space_id,
            zone_id,
            self.user.username,
            **kwargs,
        )
        return self._json(_zone_to_dict(zone))

    async def delete(self) -> web.Response:
        unauth = _ensure_authenticated(self)
        if unauth is not None:
            return unauth
        space_id = self.match("id")
        zone_id = self.match("zone_id")
        svc = self.svc(space_zone_service_key)
        await svc.delete_zone(space_id, zone_id, self.user.username)
        return web.Response(status=204)

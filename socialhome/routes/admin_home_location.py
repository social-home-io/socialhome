"""Admin › Settings › Our home location.

* ``GET    /api/admin/home-location`` → ``{latitude, longitude, from_platform}``
* ``PUT    /api/admin/home-location`` body ``{latitude, longitude}`` — set it
  (4 dp)
* ``DELETE /api/admin/home-location`` — remove it

Admin only. When the platform owns the home (Home Assistant —
``Capability.HOME_LOCATION_SYNC``) ``GET`` answers HA's value with
``from_platform: true`` and ``PUT`` / ``DELETE`` answer ``409
HOME_LOCATION_FROM_PLATFORM``. :class:`HomeLocationService` holds the rules.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import home_location_service_key
from ..domain.home_location import HomeLocation
from ..security import error_response
from .base import BaseView


def _view(home: HomeLocation) -> dict:
    return {
        "latitude": home.latitude,
        "longitude": home.longitude,
        "from_platform": home.from_platform,
    }


class AdminHomeLocationView(BaseView):
    """``/api/admin/home-location`` — read / set / clear the household home."""

    def _forbidden(self) -> web.Response | None:
        if self.user is None or not self.user.is_admin:
            return error_response(403, "FORBIDDEN", "Admin only.")
        return None

    async def get(self) -> web.Response:
        if (denied := self._forbidden()) is not None:
            return denied
        return self._json(_view(await self.svc(home_location_service_key).get()))

    async def put(self) -> web.Response:
        if (denied := self._forbidden()) is not None:
            return denied
        body = await self.body()
        if not isinstance(body, dict):
            body = {}  # → 422 INVALID_HOME_LOCATION, like a missing field
        home = await self.svc(home_location_service_key).set(
            body.get("latitude"), body.get("longitude")
        )
        return self._json(_view(home))

    async def delete(self) -> web.Response:
        if (denied := self._forbidden()) is not None:
            return denied
        return self._json(_view(await self.svc(home_location_service_key).clear()))

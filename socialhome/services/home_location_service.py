"""The household's own home location — Admin › Settings › Our home location.

Where the home comes from is the platform's business, asked through the
adapter (never ``config.mode``): when the adapter has a
:class:`~socialhome.platform.adapter.HomeLocationSource` (Home Assistant),
the home is HA's and only mirrored here — every read asks HA again, so a
change made there shows up without a restart, and setting it here is
refused. Without one (standalone) the household's admin sets it.

Either way a change is stored at 4 dp and announced as
:class:`~socialhome.domain.events.LocalHomeLocationUpdated`: the
federation service tells the paired households whose ``share_home`` is on,
and the household's own tabs redraw their map. Nothing else leaves the
household; a local-community join sends a ~1 km version only after the
person agreed (``services/local_community.py``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import LocalHomeLocationUpdated
from ..domain.home_location import (
    HomeLocation,
    HomeLocationFromPlatformError,
    validate_home_location,
)
from .bus_publisher import BusPublisherMixin

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from ..platform.adapter import HomeLocationSource
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)

_Point = tuple[float | None, float | None]


class HomeLocationService(BusPublisherMixin):
    """Read, set and clear this household's home."""

    __slots__ = ("_bus", "_federation_repo", "_source")

    def __init__(
        self,
        federation_repo: AbstractFederationRepo,
        *,
        bus: EventBus | None,
        source: HomeLocationSource | None,
    ) -> None:
        self._federation_repo = federation_repo
        self._bus = bus
        self._source = source

    async def get(self) -> HomeLocation:
        """The home as it stands — from the platform first when it has one
        (stored when it moved; kept as is when the platform has none or
        didn't answer)."""
        if self._source is not None:
            point = await self._source.get_home_location()
            if point is not None:
                await self._apply(point)
        lat, lon = await self._stored()
        return HomeLocation(lat, lon, from_platform=self._source is not None)

    async def set(self, latitude: object, longitude: object) -> HomeLocation:
        """Set the home (4 dp). Refused when the platform owns it."""
        self._require_household_owned()
        lat, lon = validate_home_location(latitude, longitude)
        await self._apply((lat, lon))
        return HomeLocation(lat, lon, from_platform=False)

    async def clear(self) -> HomeLocation:
        """Remove the home. Refused when the platform owns it."""
        self._require_household_owned()
        await self._apply((None, None))
        return HomeLocation(None, None, from_platform=False)

    def _require_household_owned(self) -> None:
        if self._source is not None:
            raise HomeLocationFromPlatformError()

    async def _stored(self) -> _Point:
        identity = await self._federation_repo.get_local_identity()
        if identity is None:
            return None, None
        return identity.get("home_lat"), identity.get("home_lon")

    async def _apply(self, point: _Point) -> None:
        """Store ``point`` and announce it — only when it changed."""
        if await self._stored() == point:
            return
        lat, lon = point
        await self._federation_repo.set_local_home_location(lat, lon)
        log.info("home location %s", "cleared" if lat is None else "updated")
        await self._emit(LocalHomeLocationUpdated(latitude=lat, longitude=lon))

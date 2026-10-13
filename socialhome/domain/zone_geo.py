"""Zone geometry — the one "which zone is this GPS point in?" helper.

Space zones (§23.8.7) and household zones are both labelled circles
(centre + ``radius_m``). Every server-side match — the space
``zone_only`` location mode, the space presence view and the household
GPS→zone match on ``POST /api/presence/location`` — goes through
:func:`match_zone`, so they agree with each other and with the SPA's
``matchZoneName`` helper (same haversine, same "nearest centre within
its radius" rule).

Pure functions, no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Final, Protocol, TypeVar

__all__ = ["EARTH_RADIUS_M", "ZoneCircle", "haversine_m", "match_zone"]

#: Earth's mean radius in metres (the haversine sphere).
EARTH_RADIUS_M: Final = 6_371_000.0


class ZoneCircle(Protocol):
    """Anything with a centre and a radius — a space or household zone."""

    @property
    def latitude(self) -> float: ...

    @property
    def longitude(self) -> float: ...

    @property
    def radius_m(self) -> int: ...


Z = TypeVar("Z", bound=ZoneCircle)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two ``(lat, lon)`` pairs."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def match_zone(zones: Iterable[Z], latitude: float, longitude: float) -> Z | None:
    """Return the zone whose centre is closest to ``(latitude, longitude)``
    among those whose distance is within their ``radius_m`` (inclusive),
    or ``None`` when the point is outside every zone. Ties keep the
    first zone in ``zones`` order."""
    best: tuple[float, Z] | None = None
    for z in zones:
        d = haversine_m(latitude, longitude, z.latitude, z.longitude)
        if d <= z.radius_m and (best is None or d < best[0]):
            best = (d, z)
    return best[1] if best is not None else None

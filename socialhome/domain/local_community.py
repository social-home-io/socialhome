"""Local communities — a public space bound to an area.

A ``public`` space may carry a centre (``spaces.lat`` / ``lon``) and a
radius (``spaces.radius_km``). That makes it a *local community* (a
neighbourhood, a village): a household may hold a seat only while the home
location it shares with the hosting household lies inside the circle.

The rules here are pure (no I/O); the one service-side check every
seat-granting path calls is :func:`socialhome.services.local_community.
require_local_eligibility`.

* Only the PUBLIC tier is ever a local community: public is discoverable
  by paired households only, never on a GFS. A private / household /
  global space with a stored pin is not gated by it.
* A pin without a radius (stored before local communities existed, when
  the create dialog took ``lat`` / ``lon`` only) is no area.
* The centre is truncated to 4 dp (the GPS rule) and the radius kept to
  one decimal between :data:`MIN_LOCAL_AREA_RADIUS_KM` and
  :data:`MAX_LOCAL_AREA_RADIUS_KM`, both on write and when a peer's
  directory entry is read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .errors import WrongTypeError
from .presence import truncate_coord
from .space import Space, SpaceType
from .zone_geo import haversine_m

__all__ = [
    "LOCAL_AREA_RADIUS_PRESETS_KM",
    "MAX_LOCAL_AREA_RADIUS_KM",
    "MIN_LOCAL_AREA_RADIUS_KM",
    "Eligibility",
    "LocalArea",
    "eligibility",
    "local_area_of",
    "local_area_wire",
    "parse_local_area_wire",
    "validate_local_area",
]

#: Smallest radius a local community may have — about a city block.
MIN_LOCAL_AREA_RADIUS_KM: Final = 0.5
#: Largest radius — a district or a small town, never a region: a wider
#: circle is no longer "local", and the area is shown to every paired
#: household.
MAX_LOCAL_AREA_RADIUS_KM: Final = 50.0
#: The radii the SPA offers as one-tap presets (any value within the
#: bounds is accepted).
LOCAL_AREA_RADIUS_PRESETS_KM: Final = (1.0, 2.0, 5.0, 10.0, 25.0)


@dataclass(slots=True, frozen=True)
class LocalArea:
    """The circle a local community's members live in."""

    lat: float
    lon: float
    radius_km: float


class Eligibility(StrEnum):
    """Where a household's home lies relative to a local area."""

    INSIDE = "inside"
    OUTSIDE = "outside"
    #: The household's home is not known here: not set, or not shared
    #: with this household.
    HOME_UNKNOWN = "home_unknown"


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WrongTypeError(f"{name} must be a number")
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ValueError(f"{name} must be finite")
    return coerced


def validate_local_area(lat: object, lon: object, radius_km: object) -> LocalArea:
    """The area ``(lat, lon, radius_km)`` names, or :class:`ValueError`.

    Numbers only (never a string, never a boolean); ``lat`` within ±90,
    ``lon`` within ±180, the radius within the bounds. The centre is
    truncated to 4 dp and the radius rounded to one decimal.
    """
    la = _number(lat, name="lat")
    lo = _number(lon, name="lon")
    r = _number(radius_km, name="radius_km")
    if not -90.0 <= la <= 90.0:
        raise ValueError("lat out of range")
    if not -180.0 <= lo <= 180.0:
        raise ValueError("lon out of range")
    if not MIN_LOCAL_AREA_RADIUS_KM <= r <= MAX_LOCAL_AREA_RADIUS_KM:
        raise ValueError(
            f"radius_km must be between {MIN_LOCAL_AREA_RADIUS_KM:g} and "
            f"{MAX_LOCAL_AREA_RADIUS_KM:g}"
        )
    # ``+ 0.0`` folds a rounded ``-0.0`` into ``0.0``.
    return LocalArea(
        lat=float(truncate_coord(la)) + 0.0,  # type: ignore[arg-type]
        lon=float(truncate_coord(lo)) + 0.0,  # type: ignore[arg-type]
        radius_km=round(r, 1),
    )


def local_area_of(space: Space) -> LocalArea | None:
    """The area ``space`` is bound to, or ``None`` when it is not a local
    community (another tier, or no complete centre + radius)."""
    if space.space_type is not SpaceType.PUBLIC:
        return None
    if space.lat is None or space.lon is None or space.radius_km is None:
        return None
    return LocalArea(
        lat=float(space.lat), lon=float(space.lon), radius_km=float(space.radius_km)
    )


def eligibility(area: LocalArea, home: tuple[float, float] | None) -> Eligibility:
    """Is ``home`` (``(lat, lon)``, or ``None`` when unknown) inside
    ``area``? The rim counts as inside."""
    if home is None:
        return Eligibility.HOME_UNKNOWN
    distance_m = haversine_m(area.lat, area.lon, home[0], home[1])
    if distance_m <= area.radius_km * 1000.0:
        return Eligibility.INSIDE
    return Eligibility.OUTSIDE


def local_area_wire(area: LocalArea) -> dict[str, float]:
    """The ``{lat, lon, radius_km}`` shape the API and the paired-household
    directory carry."""
    return {"lat": area.lat, "lon": area.lon, "radius_km": area.radius_km}


def parse_local_area_wire(raw: object) -> LocalArea | None:
    """A received ``{lat, lon, radius_km}``, validated like a local write
    (4 dp, bounds) — or ``None`` when absent or malformed: an entry with a
    broken area shows no area rather than a guessed one."""
    if not isinstance(raw, dict):
        return None
    try:
        return validate_local_area(raw.get("lat"), raw.get("lon"), raw.get("radius_km"))
    except ValueError:
        return None

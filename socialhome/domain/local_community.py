"""Local communities — a public space bound to an area.

A ``public`` space may carry a centre (``spaces.lat`` / ``lon``) and a
radius (``spaces.radius_km``). That makes it a *local community* (a
neighbourhood, a village): a household may take a seat only while its home
lies inside the circle.

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
  :data:`MAX_LOCAL_AREA_RADIUS_KM`.
* **The centre never leaves the host** (owner decision 2026-10-19: people's
  locations are not shared by design, and a neighbourhood's centre is
  usually somebody's street). Only the host's own people see it. A paired
  household's directory entry carries :func:`local_community_wire` — "a
  local community, within X km" — and nothing else.
* **The joiner's home rides the join, coarsely** (owner decision
  2026-10-19, "the joiner sends ~1 km with the join"). Every join a
  household sends to a local community carries its home rounded to
  :data:`JOIN_LOCATION_PRECISION_DP` decimals (about 1.1 km — never a
  house or a street), with the person's consent, to the host only, and the
  host decides on that point alone (:func:`join_verdict`): clearly inside,
  clearly outside, or near the edge — never on the exact distance, which
  nobody sends. The point is not stored.
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
    "JOIN_LOCATION_PRECISION_DP",
    "LOCAL_AREA_RADIUS_PRESETS_KM",
    "MAX_LOCAL_AREA_RADIUS_KM",
    "MIN_LOCAL_AREA_RADIUS_KM",
    "Eligibility",
    "LocalArea",
    "PeerLocalCommunity",
    "coarse_join_point",
    "join_location_wire",
    "join_verdict",
    "local_area_of",
    "local_area_wire",
    "local_community_wire",
    "parse_join_location",
    "parse_local_community_wire",
    "validate_local_area",
    "validate_radius_km",
]

#: Smallest radius a local community may have — about a city block.
MIN_LOCAL_AREA_RADIUS_KM: Final = 0.5
#: Largest radius — a district or a small town, never a region: a wider
#: circle is no longer "local", and the radius is shown to every paired
#: household.
MAX_LOCAL_AREA_RADIUS_KM: Final = 50.0
#: The radii the SPA offers as one-tap presets (any value within the
#: bounds is accepted).
LOCAL_AREA_RADIUS_PRESETS_KM: Final = (1.0, 2.0, 5.0, 10.0, 25.0)
#: Decimals a joining household's home is rounded to before it rides a join
#: to a local community's host: 2 dp is about 1.1 km of latitude — a part
#: of town, never a house or a street. The host rounds again on receipt, so
#: a sender that sent more is never read more precisely.
JOIN_LOCATION_PRECISION_DP: Final = 2
#: Half the side of the uncertainty cell around a rounded point: the home
#: lies within this many degrees of it on each axis.
_JOIN_HALF_CELL_DEG: Final = 0.5 * 10.0**-JOIN_LOCATION_PRECISION_DP


@dataclass(slots=True, frozen=True)
class LocalArea:
    """The circle a local community's members live in."""

    lat: float
    lon: float
    radius_km: float


@dataclass(slots=True, frozen=True)
class PeerLocalCommunity:
    """What a paired household learns about another household's local
    community: its radius. No centre, and no verdict — whether a home is
    inside is the host's decision on a join."""

    radius_km: float


class Eligibility(StrEnum):
    """Where a joining household's coarse point lies relative to a local
    area (:func:`join_verdict`)."""

    #: The whole uncertainty cell is inside the circle.
    INSIDE = "inside"
    #: The whole uncertainty cell is outside the circle.
    OUTSIDE = "outside"
    #: The cell straddles the rim: an admin decides.
    EDGE = "edge"


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WrongTypeError(f"{name} must be a number")
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ValueError(f"{name} must be finite")
    return coerced


def validate_radius_km(radius_km: object) -> float:
    """``radius_km`` within the bounds, rounded to one decimal, or
    :class:`ValueError` (a number only — never a string or a boolean)."""
    r = _number(radius_km, name="radius_km")
    if not MIN_LOCAL_AREA_RADIUS_KM <= r <= MAX_LOCAL_AREA_RADIUS_KM:
        raise ValueError(
            f"radius_km must be between {MIN_LOCAL_AREA_RADIUS_KM:g} and "
            f"{MAX_LOCAL_AREA_RADIUS_KM:g}"
        )
    return round(r, 1)


def _lat_lon(lat: object, lon: object) -> tuple[float, float]:
    la = _number(lat, name="lat")
    lo = _number(lon, name="lon")
    if not -90.0 <= la <= 90.0:
        raise ValueError("lat out of range")
    if not -180.0 <= lo <= 180.0:
        raise ValueError("lon out of range")
    return la, lo


def validate_local_area(lat: object, lon: object, radius_km: object) -> LocalArea:
    """The area ``(lat, lon, radius_km)`` names, or :class:`ValueError`.

    Numbers only (never a string, never a boolean); ``lat`` within ±90,
    ``lon`` within ±180, the radius within the bounds. The centre is
    truncated to 4 dp and the radius rounded to one decimal.
    """
    la, lo = _lat_lon(lat, lon)
    r = validate_radius_km(radius_km)
    # ``+ 0.0`` folds a rounded ``-0.0`` into ``0.0``.
    return LocalArea(
        lat=float(truncate_coord(la)) + 0.0,  # type: ignore[arg-type]
        lon=float(truncate_coord(lo)) + 0.0,  # type: ignore[arg-type]
        radius_km=r,
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


def coarse_join_point(lat: object, lon: object) -> tuple[float, float]:
    """A home rounded to :data:`JOIN_LOCATION_PRECISION_DP` decimals — the
    only shape a home ever takes on a join — or :class:`ValueError`."""
    la, lo = _lat_lon(lat, lon)
    dp = JOIN_LOCATION_PRECISION_DP
    return round(la, dp) + 0.0, round(lo, dp) + 0.0


def join_location_wire(point: tuple[float, float]) -> dict[str, float]:
    """The ``join_location`` field a join carries to a local community's
    host (inside the join's sealed payload, never anywhere else)."""
    lat, lon = coarse_join_point(point[0], point[1])
    return {"lat": lat, "lon": lon}


def parse_join_location(raw: object) -> tuple[float, float] | None:
    """A received ``join_location``, re-rounded to the join precision, or
    ``None`` when absent or malformed (refused as "no location", never
    guessed)."""
    if not isinstance(raw, dict):
        return None
    try:
        return coarse_join_point(raw.get("lat"), raw.get("lon"))
    except ValueError:
        return None


def _wrap_lon(delta: float) -> float:
    return (delta + 180.0) % 360.0 - 180.0


def join_verdict(area: LocalArea, point: tuple[float, float]) -> Eligibility:
    """Where the home behind the coarse ``point`` lies: the home is
    somewhere in the cell ±half a unit of the last decimal around it
    (:func:`coarse_join_point`), so the host answers for the whole cell.

    * INSIDE — every corner of the cell is within the radius (the rim
      counts as inside);
    * OUTSIDE — the cell's nearest point is beyond it;
    * EDGE — the cell straddles the rim: never decided automatically.
    """
    lat, lon = coarse_join_point(point[0], point[1])
    h = _JOIN_HALF_CELL_DEG
    radius_m = area.radius_km * 1000.0
    lat_lo, lat_hi = max(lat - h, -90.0), min(lat + h, 90.0)
    corners = [(a, b) for a in (lat_lo, lat_hi) for b in (lon - h, lon + h)]
    farthest = max(haversine_m(area.lat, area.lon, a, b) for a, b in corners)
    if farthest <= radius_m:
        return Eligibility.INSIDE
    near_lat = min(max(area.lat, lat_lo), lat_hi)
    near_lon = lon + min(max(_wrap_lon(area.lon - lon), -h), h)
    nearest = haversine_m(area.lat, area.lon, near_lat, near_lon)
    if nearest > radius_m:
        return Eligibility.OUTSIDE
    return Eligibility.EDGE


def local_area_wire(area: LocalArea) -> dict[str, float]:
    """The ``{lat, lon, radius_km}`` shape of the HOSTING household's own
    API (its people see and edit the centre). Never put on the wire to
    another household — that is :func:`local_community_wire`."""
    return {"lat": area.lat, "lon": area.lon, "radius_km": area.radius_km}


def local_community_wire(area: LocalArea) -> dict[str, object]:
    """The fields a ``SPACE_DIRECTORY_SYNC`` entry (and an invitation's or a
    redeem ACK's space snapshot) carries for a local community: "within X
    km". No centre, no verdict."""
    return {"local_community": True, "radius_km": area.radius_km}


def parse_local_community_wire(raw: object) -> PeerLocalCommunity | None:
    """A received entry's local-community fields, or ``None`` when the
    entry is not one or they are malformed (a broken entry shows no area
    rather than a guessed one). The radius is re-validated; anything else
    a sender put in the entry (a centre, a verdict) is ignored."""
    if not isinstance(raw, dict) or raw.get("local_community") is not True:
        return None
    try:
        radius = validate_radius_km(raw.get("radius_km"))
    except ValueError:
        return None
    return PeerLocalCommunity(radius_km=radius)

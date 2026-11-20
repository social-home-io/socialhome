"""The household's own home location — where "our home" is on the map.

One point per household (``instance_identity.home_lat`` / ``home_lon``,
4 dp). The 4 dp point stays in the household. A paired household gets
only an area of about 3 km (:func:`coarse_home_for_peer`), and only
through that pair's ``share_home`` switch; a ~1 km version rides a
local-community join only after the person agreed
(``docs/protocol/local-communities.md``).

Where it comes from is the platform's business
(:class:`~socialhome.platform.adapter.HomeLocationSource`): Home Assistant
has one of its own (its core config, the ``zone.home`` centre), which
Social Home mirrors read-only; a standalone household sets it itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import CodedError

__all__ = [
    "HOME_LOCATION_PRECISION_DP",
    "PEER_HOME_GRID_DEG",
    "HomeLocation",
    "HomeLocationFromPlatformError",
    "InvalidHomeLocationError",
    "coarse_home_for_peer",
    "coarse_home_for_peer_or_none",
    "validate_home_location",
]

#: Stored precision (§2 GPS rule: 4 dp, about 11 m).
HOME_LOCATION_PRECISION_DP = 4

#: Side of the grid cell a paired household sees our home in, in degrees
#: of latitude: 0.03° is about 3.3 km north–south. East–west the cell is
#: widened by ``1 / cos(latitude)`` so it stays about 3 km wide away from
#: the equator. The home is shown as the cell's centre, at most about
#: 2.4 km (half the cell's diagonal) from the real one — an area of town,
#: never a street (owner decision 2026-11-20).
PEER_HOME_GRID_DEG = 0.03

#: Floor for ``cos(latitude)`` when widening a cell east–west, so the cells
#: near the poles do not grow to a whole ring of longitude.
_MIN_COS_LAT = 0.1


@dataclass(slots=True, frozen=True)
class HomeLocation:
    """The household's home as the admin setting shows it.

    ``latitude`` / ``longitude`` are ``None`` when no home is set.
    ``from_platform`` — the platform provides it (Home Assistant), so it is
    shown read-only and changed there.
    """

    latitude: float | None
    longitude: float | None
    from_platform: bool


class HomeLocationFromPlatformError(CodedError):
    """The home location comes from the platform (Home Assistant) and is
    changed there, not in Social Home."""

    status = 409
    code = "HOME_LOCATION_FROM_PLATFORM"
    detail = (
        "This household's home location comes from Home Assistant. Change it there."
    )


class InvalidHomeLocationError(CodedError, ValueError):
    """Not a usable home: latitude / longitude missing, not numbers, or out
    of range (and ``0, 0``, which Home Assistant and Social Home both read
    as "not set")."""

    status = 422
    code = "INVALID_HOME_LOCATION"
    detail = "latitude must be -90..90 and longitude -180..180 (and not both 0)."


def validate_home_location(latitude: object, longitude: object) -> tuple[float, float]:
    """``(lat, lon)`` rounded to 4 dp, or :class:`InvalidHomeLocationError`."""
    values: list[float] = []
    for raw, limit in ((latitude, 90.0), (longitude, 180.0)):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidHomeLocationError()
        value = float(raw)
        if not math.isfinite(value) or abs(value) > limit:
            raise InvalidHomeLocationError()
        values.append(round(value, HOME_LOCATION_PRECISION_DP))
    lat, lon = values
    if lat == 0.0 and lon == 0.0:
        raise InvalidHomeLocationError()
    # ``+ 0.0`` folds a rounded ``-0.0`` into ``0.0``.
    return lat + 0.0, lon + 0.0


def coarse_home_for_peer(latitude: object, longitude: object) -> tuple[float, float]:
    """The home a paired household gets: the centre of the ~3 km grid cell
    (:data:`PEER_HOME_GRID_DEG`) the home lies in, at 4 dp.

    Deterministic — the same home always gives the same point, so sending
    it again (or a home that moved down the street) tells nothing more —
    and idempotent: a centre snaps to itself, so a receiver that coarsens
    an already coarse point again keeps it. Both the sender and the
    receiver run it. :class:`InvalidHomeLocationError` for a value
    :func:`validate_home_location` refuses.
    """
    lat, lon = validate_home_location(latitude, longitude)
    grid = PEER_HOME_GRID_DEG
    lat_cells = round(180.0 / grid)
    lat_cell = min(math.floor((lat + 90.0) / grid), lat_cells - 1)
    lat_centre = -90.0 + (lat_cell + 0.5) * grid
    # Whole cells around the circle of latitude, each about ``grid`` of
    # latitude wide on the ground; derived from the snapped centre so every
    # home in a lat band shares one lon grid.
    cos_lat = max(math.cos(math.radians(lat_centre)), _MIN_COS_LAT)
    lon_cells = max(1, int(360.0 * cos_lat / grid))
    lon_width = 360.0 / lon_cells
    lon_cell = min(math.floor((lon + 180.0) / lon_width), lon_cells - 1)
    lon_centre = -180.0 + (lon_cell + 0.5) * lon_width
    dp = HOME_LOCATION_PRECISION_DP
    # ``+ 0.0`` folds a rounded ``-0.0`` into ``0.0``.
    return round(lat_centre, dp) + 0.0, round(lon_centre, dp) + 0.0


def coarse_home_for_peer_or_none(
    latitude: object, longitude: object
) -> tuple[float, float] | None:
    """:func:`coarse_home_for_peer`, or ``None`` when either value is
    missing or not a usable home — for a peer home received from another
    household, where a bad value means "no home", never a precise one."""
    if latitude is None or longitude is None:
        return None
    try:
        return coarse_home_for_peer(latitude, longitude)
    except InvalidHomeLocationError:
        return None

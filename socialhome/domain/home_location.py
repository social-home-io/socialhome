"""The household's own home location — where "our home" is on the map.

One point per household (``instance_identity.home_lat`` / ``home_lon``,
4 dp). It stays in the household: it is shared with a paired household
only through that pair's ``share_home`` switch, and a ~1 km version of it
rides a local-community join only after the person agreed
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
    "HomeLocation",
    "HomeLocationFromPlatformError",
    "InvalidHomeLocationError",
    "validate_home_location",
]

#: Stored precision (§2 GPS rule: 4 dp, about 11 m).
HOME_LOCATION_PRECISION_DP = 4


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

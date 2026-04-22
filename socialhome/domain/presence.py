"""Presence / location domain types (§5.2 / §23.21).

The public API returns :class:`PersonPresence` records. GPS fields are only
populated if the user has opted in to location sharing and the reported
accuracy is ≤ 500 m (§25 / CLAUDE.md rules).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


#: The four possible presence states reported by Home Assistant.
PresenceState = Literal["home", "zone", "away", "unavailable"]


# Allowed string values — useful at runtime for validation.
PRESENCE_STATES: frozenset[str] = frozenset(
    {
        "home",
        "zone",
        "away",
        "unavailable",
    }
)


@dataclass(slots=True, frozen=True)
class PersonPresence:
    """One row of the household presence list.

    GPS fields (``latitude``, ``longitude``, ``gps_accuracy_m``) are set only
    if:

    * the user has opted in to location sharing;
    * the HA ``gps_accuracy`` attribute is known and ≤ 500 m;
    * the coordinates have been truncated to 4 decimal places.

    When any of those conditions fails the fields remain ``None``.
    """

    username: str
    user_id: str
    display_name: str
    entity_id: str  # HA person entity_id
    state: PresenceState

    picture_url: str | None = None
    zone_name: str | None = None  # set when ``state == "zone"``
    latitude: float | None = None  # 4dp-truncated
    longitude: float | None = None  # 4dp-truncated
    gps_accuracy_m: float | None = None


@dataclass(slots=True, frozen=True)
class LocationUpdate:
    """Location push payload sent by the HA integration to
    ``POST /api/presence/location``.

    The integration is responsible for truncating coordinates to 4 dp and
    dropping updates with ``gps_accuracy_m > 500``.
    """

    username: str
    state: PresenceState
    zone_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    gps_accuracy_m: float | None = None


def truncate_coord(value: float | None) -> float | None:
    """Round a GPS coordinate to 4 decimal places (§25 / CLAUDE.md).

    ``None`` passes through so callers can use this on optional
    location updates without explicit guards.
    """
    if value is None:
        return None
    return round(float(value), 4)

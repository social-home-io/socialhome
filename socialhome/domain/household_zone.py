"""Household zones — the household's own named places (Home, School…).

A household zone is a labelled circle like a space zone (§23.8.7), but
it belongs to the household and is **never federated**: the names stay
local (space-bound presence strips ``zone_name``, §23.8.6). The server
matches a member's GPS against these zones on
``POST /api/presence/location`` when the client sends no ``zone_name``
(:func:`socialhome.domain.zone_geo.match_zone`).

Field rules reuse the space-zone validators in :mod:`domain.space`
(name, 4-dp centre, 25 m – 50 km radius, ``#RRGGBB`` colour, one-emoji
icon), so both zone kinds accept exactly the same values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .errors import CodedError

__all__ = [
    "HOME_ZONE_MANAGED_DETAIL",
    "HOME_ZONE_PRESENCE_NAME",
    "MAX_HOUSEHOLD_ZONES",
    "ZONE_PALETTE",
    "ExternalHouseholdZone",
    "HouseholdZone",
    "HouseholdZoneIsHomeError",
    "HouseholdZoneLimitError",
    "HouseholdZoneMirrorResult",
    "HouseholdZoneNameTakenError",
    "HouseholdZoneNotFoundError",
    "HouseholdZoneReadOnlyError",
    "HouseholdZoneSyncStatus",
    "ZoneSourceUnavailableError",
    "zone_color",
]

#: Same cap as a space's zone catalogue — keeps the per-GPS-update match
#: trivially cheap.
MAX_HOUSEHOLD_ZONES: Final = 50

#: The presence ``zone_name`` a member in the Home zone gets, whatever
#: the zone is called — it is what makes the presence state ``home``
#: (the Home Assistant integration sends HA's ``home`` state the same way).
HOME_ZONE_PRESENCE_NAME: Final = "home"


#: :class:`HouseholdZoneReadOnlyError` detail when a zone created here
#: would take the Home flag from a synced Home.
HOME_ZONE_MANAGED_DETAIL: Final = "The Home zone is managed by Home Assistant."


@dataclass(slots=True, frozen=True)
class HouseholdZone:
    """One household zone.

    * ``id`` — opaque identifier (``hz_<urlsafe>``).
    * ``name`` — display label, unique (case-insensitive) in the household.
    * ``latitude`` / ``longitude`` — centre, truncated to 4 dp.
    * ``radius_m`` — 25 m – 50 km.
    * ``color`` — ``"#RRGGBB"`` or ``None``; ``icon`` — one emoji or ``None``.
    * ``is_home`` — the household's Home zone (at most one).
    * ``external_id`` — ``None`` for a zone created in Social Home; set
      (e.g. Home Assistant's ``zone.home``) when a platform sync owns the
      row, which makes it read-only through the API.
    * ``created_at`` / ``updated_at`` — UTC, tz-aware ISO 8601.
    """

    id: str
    name: str
    latitude: float
    longitude: float
    radius_m: int
    created_at: str
    updated_at: str
    color: str | None = None
    icon: str | None = None
    is_home: bool = False
    external_id: str | None = None

    @property
    def read_only(self) -> bool:
        """``True`` when a platform sync owns this zone."""
        return self.external_id is not None

    @property
    def presence_zone_name(self) -> str:
        """The presence ``zone_name`` for a member inside this zone."""
        return HOME_ZONE_PRESENCE_NAME if self.is_home else self.name


@dataclass(slots=True, frozen=True)
class ExternalHouseholdZone:
    """A zone as a platform sync (Home Assistant) reports it — the input
    of :meth:`HouseholdZoneService.mirror_external`. ``external_id`` is
    the platform's stable id (HA: the zone ``entity_id``)."""

    external_id: str
    name: str
    latitude: float
    longitude: float
    radius_m: int
    is_home: bool = False
    icon: str | None = None
    color: str | None = None


@dataclass(slots=True, frozen=True)
class HouseholdZoneMirrorResult:
    """What :meth:`HouseholdZoneService.mirror_external` did: rows written,
    synced rows removed, incoming zones skipped (invalid, duplicate or
    over the cap), and whether anything changed at all."""

    upserted: int
    removed: int
    skipped: int
    changed: bool


@dataclass(slots=True, frozen=True)
class HouseholdZoneSyncStatus:
    """The platform zone sync as Admin › Zones shows it.

    * ``available`` — the platform has a zone source (Home Assistant).
    * ``enabled`` — the household's option (default on).
    * ``last_synced_at`` — UTC ISO 8601 of the last run that read the
      platform, this process lifetime; ``None`` before the first one.
    * ``last_error`` — why the last run failed, ``None`` when it worked.
    """

    available: bool
    enabled: bool
    last_synced_at: str | None = None
    last_error: str | None = None


#: Zone colours for a zone without a usable one of its own — the SPA's
#: ``ZONE_PALETTE`` (``client/src/utils/zoneColor.ts``), same order.
ZONE_PALETTE: Final[tuple[str, ...]] = (
    "#3b82f6",
    "#f97316",
    "#10b981",
    "#a855f7",
    "#ec4899",
    "#facc15",
    "#14b8a6",
    "#ef4444",
    "#6366f1",
    "#84cc16",
)

_ZONE_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def zone_color(zone: HouseholdZone) -> str:
    """The colour a household zone is painted with — the SPA's
    ``zoneColor`` server-side, so the wall tablet and the app agree.

    Its own ``#RRGGBB`` when valid; otherwise a stable palette pick
    hashed from its ``external_id`` (a zone synced from Home Assistant
    keeps its colour when the row is re-created) or else its ``id``. The
    hash walks the key like JS ``for … of`` + ``charCodeAt(0)`` (code
    points; an astral one counts as its high surrogate) with ``| 0``'s
    signed 32-bit wrap.
    """
    if zone.color is not None and _ZONE_HEX.fullmatch(zone.color):
        return zone.color
    h = 0
    for ch in zone.external_id or zone.id:
        unit = ord(ch)
        if unit > 0xFFFF:
            unit = 0xD800 + ((unit - 0x10000) >> 10)
        h = (h * 31 + unit + 0x80000000) % 0x100000000 - 0x80000000
    return ZONE_PALETTE[abs(h) % len(ZONE_PALETTE)]


class HouseholdZoneNotFoundError(CodedError, LookupError):
    """No household zone with that id."""

    status = 404
    code = "NOT_FOUND"
    detail = "Zone not found."


class HouseholdZoneLimitError(CodedError):
    """The household already has :data:`MAX_HOUSEHOLD_ZONES` zones."""

    status = 409
    code = "ZONE_LIMIT"
    detail = f"The household already has {MAX_HOUSEHOLD_ZONES} zones."

    def __init__(self) -> None:
        super().__init__(params={"max": MAX_HOUSEHOLD_ZONES})


class HouseholdZoneNameTakenError(CodedError):
    """Another household zone already has that name (case-insensitive)."""

    status = 409
    code = "ZONE_NAME_TAKEN"
    detail = "A zone with this name already exists."


class HouseholdZoneReadOnlyError(CodedError):
    """The zone (or the Home flag) is managed by a platform sync such as
    Home Assistant and can't be changed here."""

    status = 409
    code = "ZONE_READ_ONLY"
    detail = "This zone is managed by Home Assistant and can't be changed here."


class HouseholdZoneIsHomeError(CodedError):
    """The Home zone can't be deleted or un-marked — mark another zone as
    Home instead."""

    status = 409
    code = "ZONE_IS_HOME"
    detail = "The Home zone can't be removed. Mark another zone as Home first."


class ZoneSourceUnavailableError(Exception):
    """A platform zone source (Home Assistant) couldn't be read this time.

    Distinct from an empty answer: a sync run that gets this skips the
    mirror and keeps the synced zones it already has — a flaky HA
    connection must never look like "HA has no zones" and wipe them."""

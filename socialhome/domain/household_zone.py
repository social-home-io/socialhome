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

from dataclasses import dataclass
from typing import Final

from .errors import CodedError

__all__ = [
    "HOME_ZONE_PRESENCE_NAME",
    "MAX_HOUSEHOLD_ZONES",
    "ExternalHouseholdZone",
    "HouseholdZone",
    "HouseholdZoneIsHomeError",
    "HouseholdZoneMirrorResult",
    "HouseholdZoneLimitError",
    "HouseholdZoneNameTakenError",
    "HouseholdZoneNotFoundError",
    "HouseholdZoneReadOnlyError",
    "ZoneSourceUnavailableError",
]

#: Same cap as a space's zone catalogue — keeps the per-GPS-update match
#: trivially cheap.
MAX_HOUSEHOLD_ZONES: Final = 50

#: The presence ``zone_name`` a member in the Home zone gets, whatever
#: the zone is called — it is what makes the presence state ``home``
#: (the Home Assistant integration sends HA's ``home`` state the same way).
HOME_ZONE_PRESENCE_NAME: Final = "home"


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

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
  :data:`MAX_LOCAL_AREA_RADIUS_KM`.
* **The centre never leaves the host** (owner decision 2026-10-19: people's
  locations are not shared by design, and a neighbourhood's centre is
  usually somebody's street). Only the host's own people see it. A paired
  household's directory entry carries :func:`local_community_wire` — "a
  local community, within X km" and the host's verdict for THAT household's
  home (the one it already chose to share) — and nothing else.
* **The directory verdict has an "unsure" band near the rim**
  (:func:`directory_verdict`): within about :func:`edge_band_km` (a tenth of
  the radius, at least 250 m) of the rim the host says neither yes nor no (``in_area: null``,
  ``area_edge: true``); the verdict is computed per grid cell
  (:func:`edge_cell`) and the band slid by a host-secret amount per cell,
  so moving a shared home step by step and watching the verdict flip does
  not trace the circle. The seat check (:func:`eligibility`) keeps the
  exact distance.
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
    "PeerLocalCommunity",
    "directory_verdict",
    "edge_band_km",
    "edge_cell",
    "edge_max_shift_km",
    "eligibility",
    "local_area_of",
    "local_area_wire",
    "local_community_wire",
    "parse_local_community_wire",
    "validate_local_area",
    "validate_radius_km",
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
#: The directory verdict's "unsure" band: this share of the radius on
#: either side of the rim …
EDGE_BAND_SHARE: Final = 0.1
#: … and never less than this (km), so a small community's rim is not
#: traced to the metre either.
MIN_EDGE_BAND_KM: Final = 0.25
#: How the directory verdict is coarsened, in bands (:func:`edge_band_km`):
#: it is computed per grid cell this many bands wide …
EDGE_CELL_BANDS: Final = 1.0
#: … its "unsure" zone is slid by a host-secret amount of up to this many
#: bands per cell …
EDGE_SHIFT_BANDS: Final = 0.5
#: … and reaches this many bands either side of the (slid) rim — enough
#: that no home in a cell can get a wrong yes or no: the shift plus the
#: cell's half-diagonal (√2 / 2) stay below it.
EDGE_UNSURE_BANDS: Final = 1.25
#: Kilometres per degree of latitude (and of longitude on the equator) on
#: the sphere :func:`~socialhome.domain.zone_geo.haversine_m` uses.
_KM_PER_DEGREE: Final = 111.19492664455873


@dataclass(slots=True, frozen=True)
class LocalArea:
    """The circle a local community's members live in."""

    lat: float
    lon: float
    radius_km: float


@dataclass(slots=True, frozen=True)
class PeerLocalCommunity:
    """What a paired household learns about another household's local
    community: its radius, and the host's verdict for the receiving
    household's own home. No centre."""

    radius_km: float
    #: ``True`` inside, ``False`` outside, ``None`` when the host does not
    #: know the receiving household's home (it does not share it) — or
    #: when it is unsure (:attr:`area_edge`).
    in_area: bool | None
    #: The receiving household's home is near the rim: the host says
    #: neither yes nor no in the directory (it decides with the exact
    #: distance when someone asks to join). Only ever ``True`` with
    #: ``in_area`` ``None``.
    area_edge: bool = False


class Eligibility(StrEnum):
    """Where a household's home lies relative to a local area."""

    INSIDE = "inside"
    OUTSIDE = "outside"
    #: The household's home is not known here: not set, or not shared
    #: with this household.
    HOME_UNKNOWN = "home_unknown"
    #: Near the rim — the directory's "unsure" answer
    #: (:func:`directory_verdict`). The seat check never returns it.
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


def validate_local_area(lat: object, lon: object, radius_km: object) -> LocalArea:
    """The area ``(lat, lon, radius_km)`` names, or :class:`ValueError`.

    Numbers only (never a string, never a boolean); ``lat`` within ±90,
    ``lon`` within ±180, the radius within the bounds. The centre is
    truncated to 4 dp and the radius rounded to one decimal.
    """
    la = _number(lat, name="lat")
    lo = _number(lon, name="lon")
    if not -90.0 <= la <= 90.0:
        raise ValueError("lat out of range")
    if not -180.0 <= lo <= 180.0:
        raise ValueError("lon out of range")
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


def eligibility(area: LocalArea, home: tuple[float, float] | None) -> Eligibility:
    """Is ``home`` (``(lat, lon)``, or ``None`` when unknown) inside
    ``area``? The rim counts as inside."""
    if home is None:
        return Eligibility.HOME_UNKNOWN
    distance_m = haversine_m(area.lat, area.lon, home[0], home[1])
    if distance_m <= area.radius_km * 1000.0:
        return Eligibility.INSIDE
    return Eligibility.OUTSIDE


def edge_band_km(radius_km: float) -> float:
    """The directory verdict's resolution — how closely a household
    probing it can place the rim, and so the centre: a tenth of the radius,
    at least :data:`MIN_EDGE_BAND_KM`. The grid, the secret shift and the
    "unsure" zone are sized in these bands (``EDGE_*_BANDS``)."""
    return max(EDGE_BAND_SHARE * float(radius_km), MIN_EDGE_BAND_KM)


def edge_max_shift_km(radius_km: float) -> float:
    """The largest secret slide of the "unsure" zone (either way)."""
    return EDGE_SHIFT_BANDS * edge_band_km(radius_km)


def _cell_km(area: LocalArea) -> float:
    """Side of a directory-verdict grid cell."""
    return EDGE_CELL_BANDS * edge_band_km(area.radius_km)


def _lon_km(lat: float) -> float:
    """Kilometres per degree of longitude at the whole-degree latitude
    ``lat`` rounds to — never the centre's, so the grid says nothing about
    where the centre is."""
    return _KM_PER_DEGREE * max(math.cos(math.radians(round(lat))), 0.01)


def edge_cell(area: LocalArea, home: tuple[float, float]) -> tuple[int, int]:
    """The grid cell ``home`` falls in: the unit the directory verdict is
    computed for (:func:`directory_verdict`). An absolute grid — it never
    depends on the centre — with cells a band wide, so every home in one
    cell gets the same answer and the same host-secret shift."""
    cell = _cell_km(area)
    lat, lon = float(home[0]), float(home[1])
    return (
        math.floor(lat * _KM_PER_DEGREE / cell),
        math.floor(lon * _lon_km(lat) / cell),
    )


def _cell_centre(area: LocalArea, home: tuple[float, float]) -> tuple[float, float]:
    cell = _cell_km(area)
    row, col = edge_cell(area, home)
    return (
        (row + 0.5) * cell / _KM_PER_DEGREE,
        (col + 0.5) * cell / _lon_km(float(home[0])),
    )


def directory_verdict(
    area: LocalArea, home: tuple[float, float] | None, *, shift_km: float
) -> Eligibility:
    """The verdict a paired household's directory entry carries for its
    shared ``home``: :func:`eligibility`, coarsened so it cannot be used to
    find the centre.

    * It is computed for the centre of the home's grid cell
      (:func:`edge_cell`), never the home itself: moving the home within a
      cell changes nothing, so stepping it along a line finds the rim only
      to a cell.
    * Within :data:`EDGE_UNSURE_BANDS` bands of the rim it is
      :attr:`Eligibility.EDGE` ("unsure") — the zone slid by ``shift_km``,
      the host's secret amount for this cell (clamped to
      :func:`edge_max_shift_km`), so where one cell turns unsure says
      little about where the rim is.
    * Outside that zone it is exact and never wrong for any home in the
      cell: "inside" is always inside the rim, "outside" always beyond it
      (the zone outweighs the cell's half-diagonal plus the shift).

    Not the seat check: every seat path still runs :func:`eligibility` on
    the exact distance.
    """
    if home is None:
        return Eligibility.HOME_UNKNOWN
    unsure_km = EDGE_UNSURE_BANDS * edge_band_km(area.radius_km)
    max_shift = edge_max_shift_km(area.radius_km)
    shift = min(max(float(shift_km), -max_shift), max_shift)
    q_lat, q_lon = _cell_centre(area, home)
    distance_km = haversine_m(area.lat, area.lon, q_lat, q_lon) / 1000.0
    if abs(distance_km - area.radius_km - shift) < unsure_km:
        return Eligibility.EDGE
    if distance_km < area.radius_km:
        return Eligibility.INSIDE
    return Eligibility.OUTSIDE


def local_area_wire(area: LocalArea) -> dict[str, float]:
    """The ``{lat, lon, radius_km}`` shape of the HOSTING household's own
    API (its people see and edit the centre). Never put on the wire to
    another household — that is :func:`local_community_wire`."""
    return {"lat": area.lat, "lon": area.lon, "radius_km": area.radius_km}


#: :class:`Eligibility` → the ``in_area`` the directory carries.
_IN_AREA: Final = {
    Eligibility.INSIDE: True,
    Eligibility.OUTSIDE: False,
    Eligibility.HOME_UNKNOWN: None,
    # Unsure: no yes or no — ``area_edge`` says why.
    Eligibility.EDGE: None,
}


def local_community_wire(area: LocalArea, verdict: Eligibility) -> dict[str, object]:
    """The fields a ``SPACE_DIRECTORY_SYNC`` entry carries for a local
    community, built for ONE recipient household: ``verdict`` is the host's
    answer for that household's shared home (``in_area``), so it is never
    put in a payload for anyone else. No centre — only the radius.

    Near the rim (:attr:`Eligibility.EDGE`) it is ``in_area: null`` with
    ``area_edge: true``: a receiver that does not know ``area_edge`` reads
    "unknown", never a wrong yes or no."""
    return {
        "local_community": True,
        "radius_km": area.radius_km,
        "in_area": _IN_AREA[verdict],
        "area_edge": verdict is Eligibility.EDGE,
    }


def parse_local_community_wire(raw: object) -> PeerLocalCommunity | None:
    """A received directory entry's local-community fields, or ``None``
    when the entry is not one or they are malformed (a broken entry shows
    no area rather than a guessed one). The radius is re-validated; an
    ``in_area`` that is not a boolean reads as unknown; ``area_edge`` counts
    only as ``true`` beside an unknown ``in_area``. Any centre a sender put
    in the entry is ignored."""
    if not isinstance(raw, dict) or raw.get("local_community") is not True:
        return None
    try:
        radius = validate_radius_km(raw.get("radius_km"))
    except ValueError:
        return None
    verdict = raw.get("in_area")
    in_area = verdict if isinstance(verdict, bool) else None
    return PeerLocalCommunity(
        radius_km=radius,
        in_area=in_area,
        area_edge=in_area is None and raw.get("area_edge") is True,
    )

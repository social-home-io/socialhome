"""A global space's place pin — an optional point, for sorting only.

Owner decision (2026-10-19): the owner or an admin of a ``global`` space
may pin it to a place (a town, a club house). The pin is the space's own
published metadata, like its name: it travels with the listing to the GFSs
that list it, and a viewer's Discover sorts by the distance from its OWN
home, computed in the viewer's browser — the viewer's location goes
nowhere. Joining never checks it.

* GLOBAL tier only. It is stored in ``spaces.lat`` / ``lon``.
* An optional **radius hint** (``radius_km``, 0.5–50 km, one decimal):
  "best for people within 10 km". A hint only — never checked at join or
  follow; a viewer's Discover compares it with the distance from its own
  home, in its browser. Stored in ``spaces.radius_km``: on a GLOBAL space
  that column is the hint, on a PUBLIC one it is a local community's area
  (:mod:`socialhome.domain.local_community` — a different thing); leaving
  either tier drops all three columns.
* Never defaulted to the household's home: the owner chooses it.
* 4 dp (the GPS rule), on write, on the GFS, and again on receipt.

Pure: no I/O. Shared by the household and the GFS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import WrongTypeError
from .local_community import validate_radius_km
from .presence import truncate_coord
from .space import Space, SpaceType

__all__ = [
    "PlacePin",
    "parse_place_pin_wire",
    "place_pin_of",
    "place_pin_wire",
    "validate_place_pin",
]


@dataclass(slots=True, frozen=True)
class PlacePin:
    """A point on the map, 4 dp, with the owner's optional radius hint."""

    lat: float
    lon: float
    #: "Best for people within this many km" — a hint, never enforced.
    radius_km: float | None = None


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WrongTypeError(f"{name} must be a number")
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ValueError(f"{name} must be finite")
    return coerced


def validate_place_pin(raw: object) -> PlacePin:
    """``{lat, lon, radius_km?}`` as a :class:`PlacePin` (truncated to 4 dp,
    the hint to one decimal within 0.5–50 km), or :class:`ValueError`
    (numbers only, within ±90 / ±180)."""
    if not isinstance(raw, dict):
        raise WrongTypeError("place must be an object {lat, lon}")
    hint = raw.get("radius_km")
    radius = None if hint is None else validate_radius_km(hint)
    lat = _number(raw.get("lat"), name="lat")
    lon = _number(raw.get("lon"), name="lon")
    if not -90.0 <= lat <= 90.0:
        raise ValueError("lat out of range")
    if not -180.0 <= lon <= 180.0:
        raise ValueError("lon out of range")
    # ``+ 0.0`` folds a rounded ``-0.0`` into ``0.0``.
    return PlacePin(
        lat=float(truncate_coord(lat)) + 0.0,  # type: ignore[arg-type]
        lon=float(truncate_coord(lon)) + 0.0,  # type: ignore[arg-type]
        radius_km=radius,
    )


def place_pin_of(space: Space) -> PlacePin | None:
    """The pin of a GLOBAL space with its radius hint, or ``None`` (another
    tier — a public space's radius is a local community's area — or no
    pin)."""
    if space.space_type is not SpaceType.GLOBAL:
        return None
    if space.lat is None or space.lon is None:
        return None
    return PlacePin(
        lat=float(space.lat),
        lon=float(space.lon),
        radius_km=float(space.radius_km) if space.radius_km is not None else None,
    )


def place_pin_wire(pin: PlacePin) -> dict[str, float]:
    """The ``{lat, lon, radius_km?}`` shape the API, the publish body and the
    GFS directory carry. ``radius_km`` only when there is a hint, so an
    unhinted pin is exactly what it was before hints existed."""
    out = {"lat": pin.lat, "lon": pin.lon}
    if pin.radius_km is not None:
        out["radius_km"] = pin.radius_km
    return out


def parse_place_pin_wire(raw: object) -> PlacePin | None:
    """A received pin, re-validated — or ``None`` when absent or malformed
    (a listing with a broken pin sorts as unpinned, never at a guess). A
    malformed radius hint alone is dropped and the pin kept: the hint only
    ever labels."""
    if raw is None:
        return None
    try:
        return validate_place_pin(raw)
    except ValueError:
        pass
    if not isinstance(raw, dict) or "radius_km" not in raw:
        return None
    try:
        return validate_place_pin({"lat": raw.get("lat"), "lon": raw.get("lon")})
    except ValueError:
        return None

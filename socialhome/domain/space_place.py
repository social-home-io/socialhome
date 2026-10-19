"""A global space's place pin — an optional point, for sorting only.

Owner decision (2026-10-19): the owner or an admin of a ``global`` space
may pin it to a place (a town, a club house). The pin is the space's own
published metadata, like its name: it travels with the listing to the GFSs
that list it, and a viewer's Discover sorts by the distance from its OWN
home, computed in the viewer's browser — the viewer's location goes
nowhere. Joining never checks it.

* GLOBAL tier only. It is stored in ``spaces.lat`` / ``lon`` with
  ``radius_km`` NULL (a radius makes the public tier's local community,
  :mod:`socialhome.domain.local_community` — a different thing).
* Never defaulted to the household's home: the owner chooses it.
* 4 dp (the GPS rule), on write, on the GFS, and again on receipt.

Pure: no I/O. Shared by the household and the GFS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import WrongTypeError
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
    """A point on the map, 4 dp."""

    lat: float
    lon: float


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WrongTypeError(f"{name} must be a number")
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ValueError(f"{name} must be finite")
    return coerced


def validate_place_pin(raw: object) -> PlacePin:
    """``{lat, lon}`` as a :class:`PlacePin` (truncated to 4 dp), or
    :class:`ValueError` (numbers only, within ±90 / ±180)."""
    if not isinstance(raw, dict):
        raise WrongTypeError("place must be an object {lat, lon}")
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
    )


def place_pin_of(space: Space) -> PlacePin | None:
    """The pin of a GLOBAL space, or ``None`` (another tier, no pin, or a
    stored radius — that is a local community's area, never a pin)."""
    if space.space_type is not SpaceType.GLOBAL:
        return None
    if space.lat is None or space.lon is None or space.radius_km is not None:
        return None
    return PlacePin(lat=float(space.lat), lon=float(space.lon))


def place_pin_wire(pin: PlacePin) -> dict[str, float]:
    """The ``{lat, lon}`` shape the API, the publish body and the GFS
    directory carry."""
    return {"lat": pin.lat, "lon": pin.lon}


def parse_place_pin_wire(raw: object) -> PlacePin | None:
    """A received pin, re-validated — or ``None`` when absent or malformed
    (a listing with a broken pin sorts as unpinned, never at a guess)."""
    if raw is None:
        return None
    try:
        return validate_place_pin(raw)
    except ValueError:
        return None

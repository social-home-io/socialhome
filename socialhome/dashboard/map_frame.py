"""Where the wall map looks: ``map.js``'s framing, in Python.

The wall-panel map picture (:mod:`.map_render`) must frame a household
exactly like the browser's Map tab, so this is a line-by-line port of
``static/map.js``'s geometry — Web Mercator at an integer zoom (the tile
grid), never closer than :data:`MAX_ZOOM`, the frame fitting every
occupied zone (centre ± radius) and everyone outside a zone into the
region the bubbles above (:data:`INSET_TOP`) and the zone names below
(:data:`INSET_BOTTOM`) leave free; someone too far for
:data:`MIN_FIT_ZOOM` stays out of the frame as an edge chip.

``tests/dashboard/map_frame_vectors.json`` holds shared cases; the
Python tests and ``client/src/dashboardMapScript.test.ts`` check both
implementations against them. Change one, change the other.

What the picture leaves out on purpose: the browser's hysteresis (it
keeps a frame across refreshes — a picture is drawn afresh, and only
when what it shows changes) and the selection (a picture has no taps).

Pure: no I/O, no pixels.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "EDGE_MARGIN",
    "INSET_BOTTOM",
    "INSET_SIDE",
    "INSET_TOP",
    "MAX_ZOOM",
    "MIN_FIT_ZOOM",
    "TILE",
    "Fit",
    "Frame",
    "Framing",
    "TileSpot",
    "best_zoom",
    "edge_point",
    "frame_for",
    "ideal_frame",
    "insets",
    "js_round",
    "meters_per_px",
    "project",
    "tile_grid",
]

TILE: Final = 256
MAX_ZOOM: Final = 16
MIN_FIT_ZOOM: Final = 9
INSET_TOP: Final = 140
INSET_BOTTOM: Final = 60
INSET_SIDE: Final = 140
#: An edge chip's distance from the box edge.
EDGE_MARGIN: Final = 10


@dataclass(slots=True, frozen=True)
class Fit:
    """One bubble as the framing sees it: its anchor, the radius it must
    keep in frame (a zone's, metres; ``0`` for a person's point), whether
    it is the Home zone and how many people it holds (the anchor when
    nobody is home is the fullest bubble)."""

    lat: float
    lon: float
    r: float
    home: bool
    members: int


@dataclass(slots=True, frozen=True)
class Frame:
    """A view: integer zoom ``z`` and the box centre in world pixels."""

    z: int
    cx: float
    cy: float

    def to_box(self, lat: float, lon: float, w: int, h: int) -> tuple[float, float]:
        """``lat``/``lon`` in box pixels (``map.js`` ``toBox``)."""
        x, y = project(lat, lon, self.z)
        return x - self.cx + w / 2, y - self.cy + h / 2

    def origin(self, w: int, h: int) -> tuple[int, int]:
        """The box's top-left in world pixels, rounded as ``map.js`` does
        before laying the tiles."""
        return js_round(self.cx - w / 2), js_round(self.cy - h / 2)


@dataclass(slots=True, frozen=True)
class Framing:
    """The frame and the indices of the fits left out of it (edge chips)."""

    frame: Frame
    far: tuple[int, ...]


@dataclass(slots=True, frozen=True)
class TileSpot:
    """One tile of the grid: ``z/x/y`` (``x`` wrapped onto the world) and
    where its top-left lands in the box."""

    z: int
    x: int
    y: int
    left: int
    top: int


def js_round(value: float) -> int:
    """JavaScript's ``Math.round`` (halves round up, toward +∞) — not
    Python's banker's rounding, or a frame could shift a pixel."""
    return math.floor(value + 0.5)


def project(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Web Mercator world pixels at zoom ``z``."""
    n = TILE * 2**z
    s = math.sin(lat * math.pi / 180)
    s = min(max(s, -0.9999), 0.9999)
    return (
        (lon + 180) / 360 * n,
        (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n,
    )


def meters_per_px(lat: float, z: int) -> float:
    return 156543.03392 * math.cos(lat * math.pi / 180) / 2**z


def insets(w: int) -> tuple[float, float, float]:
    """``(top, bottom, side)`` the chrome keeps free of anchors."""
    return INSET_TOP, INSET_BOTTOM, min(INSET_SIDE, w * 0.2)


def _bbox(fits: Sequence[Fit], z: int) -> tuple[float, float, float, float]:
    x0 = y0 = math.inf
    x1 = y1 = -math.inf
    for f in fits:
        cx, cy = project(f.lat, f.lon, z)
        r = f.r / meters_per_px(f.lat, z)
        x0, x1 = min(x0, cx - r), max(x1, cx + r)
        y0, y1 = min(y0, cy - r), max(y1, cy + r)
    return x0, y0, x1, y1


def best_zoom(fits: Sequence[Fit], w: int, h: int, min_z: int) -> int:
    """The closest zoom (≤ :data:`MAX_ZOOM`, ≥ ``min_z``) at which ``fits``
    fit the free region; ``-1`` when none does."""
    top, bottom, side = insets(w)
    for z in range(MAX_ZOOM, min_z - 1, -1):
        x0, y0, x1, y1 = _bbox(fits, z)
        if x1 - x0 <= w - 2 * side and y1 - y0 <= h - top - bottom:
            return z
    return -1


def frame_for(fits: Sequence[Fit], z: int, w: int, h: int) -> Frame:
    """The frame at zoom ``z`` that centres ``fits`` in the free region."""
    x0, y0, x1, y1 = _bbox(fits, z)
    top, bottom, _ = insets(w)
    mid = top + (h - top - bottom) / 2
    return Frame(z=z, cx=(x0 + x1) / 2, cy=(y0 + y1) / 2 - (mid - h / 2))


def ideal_frame(fits: Sequence[Fit], w: int, h: int) -> Framing:
    """Everyone in frame — or, when that needs less than
    :data:`MIN_FIT_ZOOM`, the household (Home, else the fullest bubble)
    and whoever fits with it, nearest first; the rest are far.
    ``fits`` must not be empty."""
    z = best_zoom(fits, w, h, MIN_FIT_ZOOM)
    if z >= 0:
        return Framing(frame=frame_for(fits, z, w, h), far=())
    anchor = next((i for i, f in enumerate(fits) if f.home), None)
    if anchor is None:
        anchor = 0
        for i, f in enumerate(fits):
            if f.members > fits[anchor].members:
                anchor = i
    ax, ay = project(fits[anchor].lat, fits[anchor].lon, 0)

    def distance(i: int) -> float:
        x, y = project(fits[i].lat, fits[i].lon, 0)
        return math.hypot(x - ax, y - ay)

    others = sorted((i for i in range(len(fits)) if i != anchor), key=distance)
    keep = [anchor]
    far: list[int] = []
    for i in others:
        if best_zoom([fits[k] for k in [*keep, i]], w, h, MIN_FIT_ZOOM) >= 0:
            keep.append(i)
        else:
            far.append(i)
    kept = [fits[k] for k in keep]
    z = max(best_zoom(kept, w, h, MIN_FIT_ZOOM), MIN_FIT_ZOOM)
    return Framing(frame=frame_for(kept, z, w, h), far=tuple(far))


def tile_grid(frame: Frame, w: int, h: int) -> tuple[TileSpot, ...]:
    """The tiles covering the box (``map.js`` ``drawTiles``): rows off the
    world are skipped, columns wrap around the antimeridian."""
    n = 2**frame.z
    x0, y0 = frame.origin(w, h)
    out = []
    for ty in range(y0 // TILE, (y0 + h) // TILE + 1):
        if not 0 <= ty < n:
            continue
        for tx in range(x0 // TILE, (x0 + w) // TILE + 1):
            out.append(
                TileSpot(
                    z=frame.z,
                    x=tx % n,
                    y=ty,
                    left=tx * TILE - x0,
                    top=ty * TILE - y0,
                )
            )
    return tuple(out)


def edge_point(
    px: float, py: float, w: int, h: int, chip_w: float, chip_h: float
) -> tuple[float, float, float, bool]:
    """Where an edge chip for someone at box pixel ``(px, py)`` sits
    (``map.js`` ``edgeChip``): its centre on the box edge toward them,
    the arrow's angle (degrees, ``0`` = east, clockwise) and whether it
    lies on the left / right edge (``True`` — it slides up and down to
    get clear of a marker) or the top / bottom one (slides sideways)."""
    dx, dy = px - w / 2, py - h / 2
    hx = max(1.0, w / 2 - chip_w / 2 - EDGE_MARGIN)
    hy = max(1.0, h / 2 - chip_h / 2 - EDGE_MARGIN)
    tx = hx / max(abs(dx), 1e-9)
    ty = hy / max(abs(dy), 1e-9)
    t = min(tx, ty)
    return w / 2 + dx * t, h / 2 + dy * t, math.degrees(math.atan2(dy, dx)), tx < ty

"""The wall-panel map picture: the browser's Map tab, painted with Pillow.

A wall panel can't run ``map.js``; ``GET /api/display/v1/map.png`` hands
it the map as one PNG instead. This module paints a :class:`~.map_scene.MapScene`
the way the browser lays out the Map tab (``static/map.js`` +
``dashboard.css``, same sizes in CSS pixels):

* **Frame** — :mod:`.map_frame` (the port of ``map.js``'s framing):
  Web Mercator at an integer zoom, never closer than 16; someone too far
  for a regional zoom becomes an **edge chip** pointing toward them.
* **Ground** — the tiles the caller fetched (:func:`plan_tiles` says
  which), quietened like the tablet's ``--dash-mp-tile-filter`` (inverted
  for the dark scheme). A tile that couldn't load stays the board colour.
* **Zones** — the radius as a light fill and a crisp ring (dashed when
  empty), the emoji (or first-letter) pin and the name; an empty zone
  gives way when it would land on the people or be cut by the edge.
* **Bubbles** — up to three faces (picture or initials, framed in the
  person's colour) and "+N", the tail on the zone pin or on a dot at the
  person's point; stale faces faded, a stale bubble's age on a chip.
  Bubbles that overlap merge into one whose caption names the places.
* **Credit** — the tile provider's attribution, bottom right (required
  by the OpenStreetMap licence).

Text is Roboto (bundled under ``fonts/``, SIL OFL 1.1 — the panel
firmware's own font). A label in a script Roboto lacks (Hebrew, Arabic,
CJK) is left off the picture rather than drawn as boxes; the panel's
"Who's where" list beside the map carries every name.

Everything here is sync and CPU-bound — call :func:`render_map` through
:func:`asyncio.to_thread`. The fonts load on first use, in that thread.
"""

from __future__ import annotations

import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Final

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .contrast import BLACK, WHITE, frame_color, ink_on
from .map_frame import (
    EDGE_MARGIN,
    TILE,
    Fit,
    Framing,
    TileSpot,
    edge_point,
    ideal_frame,
    tile_grid,
    meters_per_px,
)
from .map_scene import MapPalette, MapScene, SceneFace, SceneIcon

__all__ = [
    "FONT_DIR",
    "Rendered",
    "frame_scene",
    "plan_tiles",
    "render_map",
    "scene_emoji",
]

#: Roboto Regular + Bold (``OFL.txt`` alongside).
FONT_DIR: Final = Path(__file__).parent / "fonts"
#: Markers are drawn at this multiple and scaled down: smooth edges.
SS: Final = 2

# dashboard.css sizes (CSS px).
FACE = 56
FACE_RADIUS = 14
FACE_FRAME = 4
FACE_GAP = 6
BUBBLE_PAD = 8
BUBBLE_RADIUS = 22
TAIL = 12
PIN = 44
PIN_EMPTY = 36
PIN_MINI = 30
DOT = 18
LIFT_FREE = 12
LIFT_MERGED_ZONE = 19
MERGE_PAD = 8
FACES = 3
EDGE_H = 48
EDGE_FACE = 40
EDGE_FACE_RADIUS = 12
EDGE_FACE_FRAME = 3
NAME_H = 25.6  # 16 px × line-height 1.6
CAPTION_H = 22.8  # padding 2 + 16 px × 1.3
AGE_H = 22.4  # 14 px × 1.6
MAX_MERGE_ROUNDS = 4
#: Dashes on an empty zone's ring at most (50 km at zoom 16 needs ~11 000).
MAX_DASHES = 40_000

_SHADOW = (0, 0, 0, 90)


# ── Fonts and text ──────────────────────────────────────────────────────


@lru_cache(maxsize=32)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    name = "Roboto-Bold.ttf" if bold else "Roboto-Regular.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size * SS)


@lru_cache(maxsize=1)
def _notdef() -> tuple[tuple[int, int], bytes]:
    mask = _font(16, False).getmask("\U000f0000")  # a private-use point
    return mask.size, bytes(mask)


@lru_cache(maxsize=4096)
def _has_glyph(ch: str) -> bool:
    if ch.isspace():
        return True
    mask = _font(16, False).getmask(ch)
    return (mask.size, bytes(mask)) != _notdef()


def drawable(text: str) -> bool:
    """Can Roboto draw every character of ``text``?"""
    return all(_has_glyph(ch) for ch in text)


def _text_w(text: str, size: int, bold: bool) -> float:
    return _font(size, bold).getlength(text) / SS


# ── Geometry helpers ────────────────────────────────────────────────────


@dataclass(slots=True)
class _Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def overlaps(self, other: _Rect) -> bool:
        p = MERGE_PAD
        return (
            self.x0 - p < other.x1 + p
            and other.x0 - p < self.x1 + p
            and self.y0 - p < other.y1 + p
            and other.y0 - p < self.y1 + p
        )

    def shifted(self, dx: float) -> _Rect:
        return _Rect(self.x0 + dx, self.y0, self.x1 + dx, self.y1)


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    return (*_rgb(color), alpha)


def _s(v: float) -> int:
    return round(v * SS)


def _box(r: _Rect) -> tuple[int, int, int, int]:
    return _s(r.x0), _s(r.y0), _s(r.x1), _s(r.y1)


# ── Planning ────────────────────────────────────────────────────────────


def _fits(scene: MapScene) -> list[Fit]:
    return [
        Fit(lat=b.lat, lon=b.lon, r=b.radius_m, home=b.home, members=len(b.faces))
        for b in scene.bubbles
    ]


def frame_scene(scene: MapScene, w: int, h: int) -> Framing:
    """The browser's frame for ``scene`` in a ``w`` × ``h`` box. The scene
    must hold at least one bubble (a Map tab always does)."""
    return ideal_frame(_fits(scene), w, h)


def plan_tiles(scene: MapScene, w: int, h: int) -> tuple[TileSpot, ...]:
    """The tiles the picture needs, in drawing order."""
    return tile_grid(frame_scene(scene, w, h).frame, w, h)


def scene_emoji(scene: MapScene) -> frozenset[str]:
    """Every emoji code the picture may draw."""
    icons = [b.icon for b in scene.bubbles] + [p.icon for p in scene.pins]
    return frozenset(i.code for i in icons if i.code is not None)


# ── The ground ──────────────────────────────────────────────────────────


def _lut(brightness: float, contrast: float) -> list[int]:
    """CSS ``brightness()`` then ``contrast()`` as a per-channel table."""
    out = []
    for v in range(256):
        x = v / 255 * brightness
        x = (x - 0.5) * contrast + 0.5
        out.append(max(0, min(255, round(x * 255))))
    return out


def _quiet(img: Image.Image, dark: bool) -> Image.Image:
    """``--dash-mp-tile-filter``: light — saturate(0.4) brightness(1.04)
    contrast(0.88); dark — invert, hue-rotate(180°), saturate(0.3)
    brightness(0.8) contrast(0.85)."""
    rgb = img.convert("RGB")
    if dark:
        rgb = ImageChops.invert(rgb)
        h, s, v = rgb.convert("HSV").split()
        h = h.point(lambda x: (x + 128) % 256)
        rgb = Image.merge("HSV", (h, s, v)).convert("RGB")
        rgb = ImageEnhance.Color(rgb).enhance(0.3)
        rgb = rgb.point(_lut(0.8, 0.85) * 3)
    else:
        rgb = ImageEnhance.Color(rgb).enhance(0.4)
        rgb = rgb.point(_lut(1.04, 0.88) * 3)
    out = rgb.convert("RGBA")
    out.putalpha(img.getchannel("A"))
    return out


def _ground(
    grid: Sequence[TileSpot],
    tiles: Mapping[tuple[int, int, int], bytes],
    w: int,
    h: int,
    palette: MapPalette,
    dark: bool,
) -> tuple[Image.Image, tuple[tuple[int, int, int], ...]]:
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    missing: list[tuple[int, int, int]] = []
    for spot in grid:
        key = (spot.z, spot.x, spot.y)
        body = tiles.get(key)
        img = _decode(body, (TILE, TILE)) if body is not None else None
        if img is None:
            if key not in missing:
                missing.append(key)
            continue
        layer.paste(img, (spot.left, spot.top))
    base = Image.new("RGBA", (w, h), _rgba(palette.bg))
    base.alpha_composite(_quiet(layer, dark))
    return base, tuple(sorted(missing))


def _decode(body: bytes, size: tuple[int, int] | None = None) -> Image.Image | None:
    """An image as RGBA (scaled to ``size``), ``None`` when unreadable."""
    try:
        with Image.open(io.BytesIO(body)) as img:
            img.load()
            out = img.convert("RGBA")
    except Exception:
        return None
    if size is not None and out.size != size:
        out = out.resize(size, Image.Resampling.LANCZOS)
    return out


# ── Markers: layout ─────────────────────────────────────────────────────


@dataclass(slots=True)
class _Unit:
    """A bubble on screen — one of the scene's, or several merged."""

    parts: list[int]
    faces: list[SceneFace]
    anchor_x: float
    anchor_y: float
    lift: float
    caption: list[tuple[str, SceneIcon | None]] | None
    age: str | None
    rect: _Rect = field(default_factory=lambda: _Rect(0, 0, 0, 0))


@dataclass(slots=True)
class _ZoneMark:
    index: int
    x: float
    y: float
    size: float
    mini: bool = False
    off: bool = False
    name_cut: bool = False


def _faces_w(n_faces: int) -> float:
    shown = min(n_faces, FACES) + (1 if n_faces > FACES else 0)
    return shown * FACE + (shown - 1) * FACE_GAP


def _caption_w(caption: Sequence[tuple[str, SceneIcon | None]]) -> float:
    return sum(_run_w(run, 16) for run in caption)


def _run_w(run: tuple[str, SceneIcon | None], size: int) -> float:
    text, icon = run
    if icon is not None:
        return _icon_w(icon, size)
    return _text_w(text, size, True)


def _icon_w(icon: SceneIcon, size: int) -> float:
    if icon.code is not None:
        return size * 1.2
    return _text_w(icon.text, size, True)


def _size_unit(u: _Unit, w: int) -> None:
    width = _faces_w(len(u.faces))
    height: float = FACE
    if u.caption is not None:
        width = max(width, _caption_w(u.caption))
        height += CAPTION_H
    width += 2 * BUBBLE_PAD
    height += 2 * BUBBLE_PAD
    bottom = u.anchor_y - u.lift - TAIL
    rect = _Rect(
        u.anchor_x - width / 2, bottom - height, u.anchor_x + width / 2, bottom
    )
    # keepIn: nudged sideways back into the box.
    if rect.x0 < 4:
        rect = rect.shifted(4 - rect.x0)
    elif rect.x1 > w - 4:
        rect = rect.shifted(w - 4 - rect.x1)
    u.rect = rect


def _merged_caption(
    scene: MapScene, parts: Sequence[int]
) -> list[tuple[str, SceneIcon | None]]:
    """``map.js`` ``caption``: up to two places, "+N places", then
    "🚗 Away" once."""
    places: list[tuple[str, SceneIcon]] = []
    away: tuple[str, SceneIcon] | None = None
    seen: set[str] = set()
    for i in parts:
        b = scene.bubbles[i]
        item = (b.place, b.icon)
        if b.away:
            away = item
        elif f"{b.icon.text} {b.place}" not in seen:
            seen.add(f"{b.icon.text} {b.place}")
            places.append(item)
    out: list[list[tuple[str, SceneIcon | None]]] = [
        [("", icon), (" " + place, None)] for place, icon in places[:2]
    ]
    rest = len(places) - 2
    if rest > 0:
        more = (
            scene.more_one if rest == 1 else scene.more_other.replace("{n}", str(rest))
        )
        out.append([(more, None)])
    if away is not None:
        out.append([("", away[1]), (" " + away[0], None)])
    runs: list[tuple[str, SceneIcon | None]] = []
    for k, piece in enumerate(out):
        if k:
            runs.append((" · ", None))
        runs.extend(piece)
    return runs


def _merge(
    scene: MapScene,
    units: list[_Unit],
    anchors: Mapping[int, tuple[float, float]],
    w: int,
) -> list[_Unit]:
    """``map.js`` ``mergeOverlaps``: overlapping bubbles become one, a few
    rounds at most."""
    for _ in range(MAX_MERGE_ROUNDS):
        root = list(range(len(units)))

        def find(i: int) -> int:
            while root[i] != i:
                i = root[i]
            return i

        joined = False
        for i in range(len(units)):
            for j in range(i + 1, len(units)):
                if find(i) != find(j) and units[i].rect.overlaps(units[j].rect):
                    root[find(j)] = find(i)
                    joined = True
        if not joined:
            break
        clusters: dict[int, list[_Unit]] = {}
        for k, u in enumerate(units):
            clusters.setdefault(find(k), []).append(u)
        merged: list[_Unit] = []
        for cluster in clusters.values():
            if len(cluster) == 1:
                merged.append(cluster[0])
                continue
            parts = sorted(p for u in cluster for p in u.parts)
            faces: list[SceneFace] = []
            ids: set[str] = set()
            for p in parts:
                for f in scene.bubbles[p].faces:
                    if f.user_id not in ids:
                        ids.add(f.user_id)
                        faces.append(f)
            spots = [anchors[p] for p in parts]
            zoned = any(scene.bubbles[p].zone is not None for p in parts)
            unit = _Unit(
                parts=parts,
                faces=faces,
                anchor_x=sum(a[0] for a in spots) / len(spots),
                anchor_y=min(a[1] for a in spots),
                lift=LIFT_MERGED_ZONE if zoned else LIFT_FREE,
                caption=_merged_caption(scene, parts),
                age=None,
            )
            _size_unit(unit, w)
            merged.append(unit)
        units = merged
    return units


# ── Markers: painting ───────────────────────────────────────────────────


def _rounded(
    draw: ImageDraw.ImageDraw,
    rect: _Rect,
    radius: float,
    fill: tuple[int, ...] | None,
    outline: tuple[int, ...] | None = None,
    width: float = 0,
    corners: tuple[bool, bool, bool, bool] | None = None,
) -> None:
    draw.rounded_rectangle(
        _box(rect),
        radius=_s(radius),
        fill=fill,
        outline=outline,
        width=_s(width) if outline is not None else 0,
        corners=corners,
    )


def _faded(img: Image.Image) -> Image.Image:
    """``opacity: 0.5; filter: grayscale(0.8)``."""
    gray = img.convert("L").convert("RGBA")
    out = Image.blend(img, gray, 0.8)
    out.putalpha(img.getchannel("A").point(lambda a: a // 2))
    return out


def _face(
    face: SceneFace,
    size: float,
    radius: float,
    frame: float,
    palette: MapPalette,
    dark: bool,
    pictures: Mapping[str, Image.Image],
    font_size: int,
) -> Image.Image:
    """A rounded-square face: picture (or initials on the person's
    colour) inside a frame in their colour, nudged to 3:1 on the bubble."""
    px = _s(size)
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = face.color or palette.muted
    edge = (
        frame_color(face.color, palette.bubble, WHITE if dark else BLACK)
        if face.color
        else palette.muted
    )
    whole = _Rect(0, 0, size, size)
    _rounded(draw, whole, radius, _rgba(edge))
    inner = _Rect(frame, frame, size - frame, size - frame)
    _rounded(draw, inner, max(radius - frame, 0), _rgba(fill))
    picture = pictures.get(face.user_id)
    if picture is not None:
        side = _s(size - 2 * frame)
        pic = picture.resize((side, side), Image.Resampling.LANCZOS)
        mask = Image.new("L", (side, side), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, side - 1, side - 1), radius=_s(max(radius - frame, 0)), fill=255
        )
        pic_alpha = ImageChops.multiply(pic.getchannel("A"), mask)
        img.paste(pic, (_s(frame), _s(frame)), pic_alpha)
    elif face.initials and drawable(face.initials):
        draw.text(
            (px / 2, px / 2),
            face.initials,
            font=_font(font_size, True),
            fill=_rgba(ink_on(fill)),
            anchor="mm",
        )
    return _faded(img) if face.stale else img


def _paste(canvas: Image.Image, img: Image.Image, x: float, y: float) -> None:
    """``img`` over ``canvas`` at CSS ``(x, y)`` — clipped at every edge."""
    dx, dy = _s(x), _s(y)
    left, top = max(dx, 0), max(dy, 0)
    right = min(dx + img.width, canvas.width)
    bottom = min(dy + img.height, canvas.height)
    if right <= left or bottom <= top:
        return
    canvas.alpha_composite(
        img.crop((left - dx, top - dy, right - dx, bottom - dy)), (left, top)
    )


def _emoji_img(
    icon: SceneIcon, size: float, emoji: Mapping[str, Image.Image]
) -> Image.Image | None:
    if icon.code is None:
        return None
    src = emoji.get(icon.code)
    if src is None:
        return None
    return src.resize((_s(size), _s(size)), Image.Resampling.LANCZOS)


def _runs(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    runs: Sequence[tuple[str, SceneIcon | None]],
    x: float,
    cy: float,
    size: int,
    color: str,
    emoji: Mapping[str, Image.Image],
) -> None:
    """Text and emoji in a row, vertically centred on ``cy``."""
    for run in runs:
        text, icon = run
        if icon is not None:
            img = _emoji_img(icon, size * 1.2, emoji)
            if img is not None:
                _paste(canvas, img, x, cy - size * 0.6)
            else:
                draw.text(
                    (_s(x), _s(cy)),
                    icon.text,
                    font=_font(size, True),
                    fill=_rgba(color),
                    anchor="lm",
                )
        else:
            draw.text(
                (_s(x), _s(cy)),
                text,
                font=_font(size, True),
                fill=_rgba(color),
                anchor="lm",
            )
        x += _run_w(run, size)


def _runs_drawable(runs: Sequence[tuple[str, SceneIcon | None]]) -> bool:
    return all(
        drawable(text if icon is None else ("" if icon.code else icon.text))
        for text, icon in runs
    )


def _ellipsize(text: str, size: int, room: float) -> str:
    if _text_w(text, size, True) <= room:
        return text
    while text and _text_w(text + "…", size, True) > room:
        text = text[:-1]
    return text.rstrip() + "…" if text else ""


@dataclass(slots=True)
class _Chip:
    unit_index: int
    rect: _Rect
    angle: float
    label: list[tuple[str, SceneIcon | None]]


def _arrow(angle: float, size: float, color: str) -> Image.Image:
    """``➜`` in the accent colour, turned toward ``angle`` degrees."""
    px = _s(size)
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s = px
    shaft = [
        (0.12 * s, 0.42 * s),
        (0.55 * s, 0.42 * s),
        (0.55 * s, 0.58 * s),
        (0.12 * s, 0.58 * s),
    ]
    head = [(0.5 * s, 0.2 * s), (0.92 * s, 0.5 * s), (0.5 * s, 0.8 * s)]
    draw.polygon(shaft, fill=_rgba(color))
    draw.polygon(head, fill=_rgba(color))
    return img.rotate(-angle, resample=Image.Resampling.BICUBIC)


# ── The picture ─────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Rendered:
    """A painted map: the PNG and the tiles that couldn't be drawn."""

    png: bytes
    missing: tuple[tuple[int, int, int], ...]


def render_map(
    scene: MapScene,
    *,
    w: int,
    h: int,
    palette: MapPalette,
    dark: bool,
    tiles: Mapping[tuple[int, int, int], bytes],
    pictures: Mapping[str, bytes],
    emoji: Mapping[str, bytes],
) -> Rendered:
    """Paint ``scene`` as a ``w`` × ``h`` PNG. ``tiles`` maps ``(z, x, y)``
    to tile bytes (absent → the board colour shows), ``pictures`` a user id
    to their square picture, ``emoji`` a Twemoji code to its PNG. Sync and
    CPU-bound — run it in a worker thread."""
    framing = frame_scene(scene, w, h)
    frame = framing.frame
    base, missing = _ground(tile_grid(frame, w, h), tiles, w, h, palette, dark)

    def at(lat: float, lon: float) -> tuple[float, float]:
        return frame.to_box(lat, lon, w, h)  # map.js toBox: unrounded

    pics = {k: img for k, v in pictures.items() if (img := _decode(v)) is not None}
    emo = {k: img for k, v in emoji.items() if (img := _decode(v)) is not None}

    anchors = {i: at(b.lat, b.lon) for i, b in enumerate(scene.bubbles)}
    far = set(framing.far)

    # Zones.
    marks: list[_ZoneMark] = []
    for k, p in enumerate(scene.pins):
        x, y = at(p.lat, p.lon)
        marks.append(
            _ZoneMark(index=k, x=x, y=y, size=PIN if p.occupied else PIN_EMPTY)
        )
    by_zone = {scene.pins[m.index].zone: m for m in marks}

    # Bubbles (the far ones are edge chips instead).
    units: list[_Unit] = []
    for i, b in enumerate(scene.bubbles):
        if i in far:
            continue
        ax, ay = anchors[i]
        unit = _Unit(
            parts=[i],
            faces=list(b.faces),
            anchor_x=ax,
            anchor_y=ay,
            lift=(PIN / 2 + 4) if b.zone is not None else LIFT_FREE,
            caption=None,
            age=b.age,
        )
        _size_unit(unit, w)
        units.append(unit)
    units = _merge(scene, units, anchors, w)
    for u in units:
        if len(u.parts) > 1:
            for part in u.parts:
                zone = scene.bubbles[part].zone
                if zone is not None and zone in by_zone:
                    by_zone[zone].mini = True
                    by_zone[zone].size = PIN_MINI

    def zone_rect(m: _ZoneMark, pin_only: bool = False) -> _Rect:
        half = m.size / 2
        rect = _Rect(m.x - half, m.y - half, m.x + half, m.y + half)
        if pin_only or m.mini or m.name_cut:
            return rect
        pin = scene.pins[m.index]
        name_w = _text_w(pin.name, 16, True) + 20
        return _Rect(
            min(rect.x0, m.x - name_w / 2),
            rect.y0,
            max(rect.x1, m.x + name_w / 2),
            rect.y1 + 4 + NAME_H,
        )

    def name_rect(m: _ZoneMark) -> _Rect:
        pin = scene.pins[m.index]
        name_w = _text_w(pin.name, 16, True) + 20
        top = m.y + m.size / 2 + 4
        return _Rect(m.x - name_w / 2, top, m.x + name_w / 2, top + NAME_H)

    dots: list[tuple[int, float, float]] = [
        (i, *anchors[i])
        for i, b in enumerate(scene.bubbles)
        if b.zone is None and i not in far
    ]

    # Edge chips for the far ones, slid clear of markers.
    chips: list[_Chip] = []
    for i in sorted(far):
        b = scene.bubbles[i]
        label: list[tuple[str, SceneIcon | None]] = [
            (b.names + " · ", None),
            ("", b.icon),
            (" " + b.place, None),
        ]
        if not _runs_drawable(label):
            label = []
        room = w * 0.6 - (4 + 20 + 8 + EDGE_FACE + 8 + 14)
        width_label = sum(_run_w(r, 16) for r in label)
        if width_label > room:
            label = [(_ellipsize(b.names + " · " + b.place, 16, room), None)]
            width_label = _run_w(label[0], 16)
        chip_w = 4 + 20 + 8 + EDGE_FACE + (8 + width_label if label else 0) + 14
        px, py = anchors[i]
        cx, cy, angle, along_y = edge_point(px, py, w, h, chip_w, EDGE_H)

        def chip_at(x: float, y: float, cw: float = chip_w) -> _Rect:
            return _Rect(x - cw / 2, y - EDGE_H / 2, x + cw / 2, y + EDGE_H / 2)

        blockers = (
            [u.rect for u in units]
            + [c.rect for c in chips]
            + [zone_rect(m) for m in marks if not m.off]
        )
        spot = chip_at(cx, cy)
        for k in range(61):
            step = (1 if k % 2 else -1) * math.ceil(k / 2) * 12
            if along_y:
                x = cx
                y = min(
                    max(cy + step, EDGE_H / 2 + EDGE_MARGIN),
                    h - EDGE_H / 2 - EDGE_MARGIN,
                )
            else:
                x = min(
                    max(cx + step, chip_w / 2 + EDGE_MARGIN),
                    w - chip_w / 2 - EDGE_MARGIN,
                )
                y = cy
            candidate = chip_at(x, y)
            if not any(candidate.overlaps(r) for r in blockers):
                spot = candidate
                break
        chips.append(_Chip(unit_index=i, rect=spot, angle=angle, label=label))

    # Empty zones give way to the people and to the edge.
    busy = (
        [u.rect for u in units]
        + [zone_rect(m) for m in marks if scene.pins[m.index].occupied and not m.off]
        + [
            _Rect(x - DOT / 2, y - DOT / 2, x + DOT / 2, y + DOT / 2)
            for _, x, y in dots
        ]
    )
    for m in marks:
        if scene.pins[m.index].occupied:
            continue
        r = zone_rect(m, pin_only=True)
        cut = r.x0 < 0 or r.y0 < 0 or r.x1 > w or r.y1 > h
        if cut or any(r.overlaps(b) for b in busy):
            m.off = True
    for m in marks:
        if m.mini or m.off:
            continue
        r = name_rect(m)
        if r.x0 < 4 or r.y0 < 4 or r.x1 > w - 4 or r.y1 > h - 4:
            m.name_cut = True

    # Paint, bottom to top, at SS× then scale down.
    over = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)
    for m in marks:
        if m.off:
            continue
        pin = scene.pins[m.index]
        color = pin.color or palette.muted
        radius = pin.radius_m / meters_per_px(pin.lat, frame.z)
        area = _Rect(m.x - radius, m.y - radius, m.x + radius, m.y + radius)
        draw.ellipse(_box(area), fill=_rgba(color, 20 if not pin.occupied else 41))
        if pin.occupied:
            draw.ellipse(_box(area), outline=_rgba(color), width=_s(2))
        else:
            _dashed_circle(draw, m.x, m.y, radius, color, w, h)

    # Shadows of the pins and dots, then the pins and dots.
    _layer(
        over,
        [(zone_rect(m, pin_only=True), m.size / 2) for m in marks if not m.off]
        + [
            (_Rect(x - DOT / 2, y - DOT / 2, x + DOT / 2, y + DOT / 2), DOT / 2)
            for _, x, y in dots
        ],
        dy=1,
        blur=3,
    )
    for m in marks:
        if m.off:
            continue
        _zone_pin(over, draw, scene, m, palette, emo)
    for i, x, y in dots:
        b = scene.bubbles[i]
        face = b.faces[0]
        fill = (
            frame_color(face.color, palette.bubble, WHITE if dark else BLACK)
            if face.color
            else palette.muted
        )
        dot = Image.new("RGBA", (_s(DOT), _s(DOT)), (0, 0, 0, 0))
        ImageDraw.Draw(dot).ellipse(
            (0, 0, _s(DOT) - 1, _s(DOT) - 1),
            fill=_rgba(fill),
            outline=_rgba(WHITE),
            width=_s(3),
        )
        _paste(over, _faded(dot) if b.stale else dot, x - DOT / 2, y - DOT / 2)

    # Bubbles.
    _layer(over, [(u.rect, BUBBLE_RADIUS) for u in units], dy=2, blur=6)
    for u in units:
        _bubble(over, draw, u, palette, dark, pics, emo)

    # Edge chips.
    _layer(over, [(c.rect, EDGE_H / 2) for c in chips], dy=2, blur=6)
    for c in chips:
        _chip(over, draw, scene, c, palette, dark, pics, emo)

    # The tile credit.
    _attribution(over, draw, scene.attribution, w, h, palette)

    small = (
        over.convert("RGBa").resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")
    )
    base.alpha_composite(small)
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG")
    return Rendered(png=out.getvalue(), missing=missing)


def _layer(
    canvas: Image.Image, rects: Sequence[tuple[_Rect, float]], *, dy: float, blur: float
) -> None:
    """Soft drop shadows under ``rects`` — each blurred on its own small
    patch (blurring the whole canvas would cost a big picture dearly)."""
    pad = 3 * blur
    for rect, radius in rects:
        patch = _Rect(
            rect.x0 - pad, rect.y0 - pad + dy, rect.x1 + pad, rect.y1 + pad + dy
        )
        img = Image.new("RGBA", (_s(patch.x1 - patch.x0), _s(patch.y1 - patch.y0)))
        _rounded(
            ImageDraw.Draw(img),
            _Rect(pad, pad, pad + rect.x1 - rect.x0, pad + rect.y1 - rect.y0),
            radius,
            _SHADOW,
        )
        _paste(
            canvas, img.filter(ImageFilter.GaussianBlur(_s(blur))), patch.x0, patch.y0
        )


def _dashed_circle(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    r: float,
    color: str,
    w: int,
    h: int,
) -> None:
    """``border: 2px dashed`` on a circle: ~6 px dashes, drawn as short
    lines — only those inside the box (a 50 km zone at zoom 16 is a ring
    of thousands of dashes, nearly all off the picture)."""
    if r <= 0:
        return
    steps = min(max(8, int(2 * math.pi * r / 6) // 2 * 2), MAX_DASHES)
    span = 2 * math.pi / steps
    fill = _rgba(color)
    for k in range(0, steps, 2):
        points = [
            (x + r * math.cos(a), y + r * math.sin(a))
            for a in (k * span, (k + 0.5) * span, (k + 1) * span)
        ]
        if all(px < -4 or px > w + 4 or py < -4 or py > h + 4 for px, py in points):
            continue
        draw.line([(_s(px), _s(py)) for px, py in points], fill=fill, width=_s(2))


def _zone_pin(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: MapScene,
    m: _ZoneMark,
    palette: MapPalette,
    emoji: Mapping[str, Image.Image],
) -> None:
    pin = scene.pins[m.index]
    color = pin.color or palette.muted
    border = 2 if m.mini else 3
    r = _Rect(m.x - m.size / 2, m.y - m.size / 2, m.x + m.size / 2, m.y + m.size / 2)
    draw.ellipse(_box(r), fill=_rgba(color), outline=_rgba(WHITE), width=_s(border))
    font_size = 15 if m.mini else (18 if not pin.occupied else 22)
    img = _emoji_img(pin.icon, font_size * 1.15, emoji)
    if img is not None:
        _paste(canvas, img, m.x - font_size * 0.575, m.y - font_size * 0.575)
    elif pin.icon.code is None and drawable(pin.icon.text):
        draw.text(
            (_s(m.x), _s(m.y)),
            pin.icon.text,
            font=_font(font_size, True),
            fill=_rgba(ink_on(color)),
            anchor="mm",
        )
    if m.mini or m.name_cut or not drawable(pin.name):
        return
    name_w = _text_w(pin.name, 16, True) + 20
    top = m.y + m.size / 2 + 4
    pill = _Rect(m.x - name_w / 2, top, m.x + name_w / 2, top + NAME_H)
    _rounded(draw, pill, NAME_H / 2, _rgba(palette.surface))
    draw.text(
        (_s(m.x), _s(top + NAME_H / 2)),
        pin.name,
        font=_font(16, True),
        fill=_rgba(palette.text if pin.occupied else palette.muted),
        anchor="mm",
    )


def _bubble(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    u: _Unit,
    palette: MapPalette,
    dark: bool,
    pictures: Mapping[str, Image.Image],
    emoji: Mapping[str, Image.Image],
) -> None:
    r = u.rect
    fill = _rgba(palette.bubble)
    _rounded(draw, r, BUBBLE_RADIUS, fill)
    # The tail, centred under the bubble (a nudged bubble's tail points a
    # little off its anchor, as on the tablet).
    tail_w = TAIL * 1.6
    cx = (r.x0 + r.x1) / 2
    draw.polygon(
        [
            (_s(cx - tail_w / 2), _s(r.y1 - 1)),
            (_s(cx + tail_w / 2), _s(r.y1 - 1)),
            (_s(cx), _s(r.y1 + TAIL - 1)),
        ],
        fill=fill,
    )
    row_w = _faces_w(len(u.faces))
    x = (r.x0 + r.x1) / 2 - row_w / 2
    y = r.y0 + BUBBLE_PAD
    for face in u.faces[:FACES]:
        img = _face(face, FACE, FACE_RADIUS, FACE_FRAME, palette, dark, pictures, 24)
        _paste(canvas, img, x, y)
        x += FACE + FACE_GAP
    if len(u.faces) > FACES:
        more = _Rect(x, y, x + FACE, y + FACE)
        _rounded(
            draw, more, FACE_RADIUS, _rgba(palette.surface), _rgba(palette.border), 1
        )
        draw.text(
            (_s(x + FACE / 2), _s(y + FACE / 2)),
            f"+{len(u.faces) - FACES}",
            font=_font(20, True),
            fill=_rgba(palette.text),
            anchor="mm",
        )
    if u.caption is not None and _runs_drawable(u.caption):
        cw = _caption_w(u.caption)
        _runs(
            canvas,
            draw,
            u.caption,
            (r.x0 + r.x1) / 2 - cw / 2,
            y + FACE + 2 + 20.8 / 2,
            16,
            palette.text,
            emoji,
        )
    if u.age and drawable(u.age):
        aw = _text_w(u.age, 14, True) + 16 + 2
        chip = _Rect(r.x1 + 10 - aw, r.y0 - 10, r.x1 + 10, r.y0 - 10 + AGE_H + 2)
        _rounded(
            draw,
            chip,
            (AGE_H + 2) / 2,
            _rgba(palette.surface),
            _rgba(palette.border),
            1,
        )
        draw.text(
            (_s((chip.x0 + chip.x1) / 2), _s((chip.y0 + chip.y1) / 2)),
            u.age,
            font=_font(14, True),
            fill=_rgba(palette.text),
            anchor="mm",
        )


def _chip(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: MapScene,
    c: _Chip,
    palette: MapPalette,
    dark: bool,
    pictures: Mapping[str, Image.Image],
    emoji: Mapping[str, Image.Image],
) -> None:
    r = c.rect
    _rounded(draw, r, EDGE_H / 2, _rgba(palette.surface), _rgba(palette.border), 1)
    cy = (r.y0 + r.y1) / 2
    x = r.x0 + 4
    _paste(canvas, _arrow(c.angle, 20, palette.accent), x, cy - 10)
    x += 20 + 8
    face = scene.bubbles[c.unit_index].faces[0]
    img = _face(
        face, EDGE_FACE, EDGE_FACE_RADIUS, EDGE_FACE_FRAME, palette, dark, pictures, 18
    )
    _paste(canvas, img, x, cy - EDGE_FACE / 2)
    x += EDGE_FACE + 8
    if c.label:
        _runs(canvas, draw, c.label, x, cy, 16, palette.text, emoji)


def _attribution(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    text: str,
    w: int,
    h: int,
    palette: MapPalette,
) -> None:
    """Bottom right, like ``.dash-mp-attrib`` — always drawn."""
    tw = _text_w(text, 12, False)
    box = _Rect(w - tw - 16, h - 19, w, h)
    r, g, b = _rgb(palette.surface)
    draw.rounded_rectangle(
        _box(box),
        radius=_s(8),
        fill=(r, g, b, 217),
        corners=(True, False, False, False),
    )
    draw.text(
        (_s(w - 8), _s(h - 9.5)),
        text,
        font=_font(12, False),
        fill=_rgba(palette.muted),
        anchor="rm",
    )

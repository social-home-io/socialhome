"""What the wall-panel map picture draws — the Map tab's view, as data.

:func:`map_scene` turns the Map tab's view model (:mod:`.map_view` — the
same one the browser's Map tab and ``GET /api/display/v1/map`` spell, so
hidden people, stale fading and the 24 h drop are already applied) into
the plain values :mod:`.map_render` paints: bubbles with their faces,
zone pins, the strings a merged caption needs and the tile credit.
Nothing else reaches the picture.

:func:`scene_key` hashes everything that decides the pixels — the scene,
the colours of both schemes, which tile source draws the ground and
:data:`RENDER_VERSION` — and :func:`scene_etag` makes it the picture's
``ETag`` for one size and scheme; ``GET /api/display/v1/map`` names the
default picture's up front.

Pure: no I/O, no pixels.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Final

import orjson

from .contrast import BUBBLE_DARK, BUBBLE_LIGHT, hex_color
from .emoji import emoji_code
from .map_view import MapView, pin_icon

__all__ = [
    "RENDER_VERSION",
    "MapPalette",
    "MapScene",
    "SceneBubble",
    "SceneFace",
    "SceneIcon",
    "ScenePin",
    "map_palettes",
    "map_scene",
    "scene_etag",
    "scene_key",
]

#: Bump when :mod:`.map_render` draws the same scene differently, so
#: panels holding the old picture fetch the new one.
RENDER_VERSION: Final = 1


@dataclass(slots=True, frozen=True)
class SceneIcon:
    """A place glyph: the emoji's Twemoji ``code`` (drawn as the bundled
    picture), else ``None`` and ``text`` is a letter."""

    text: str
    code: str | None


@dataclass(slots=True, frozen=True)
class SceneFace:
    """One person in a bubble: their picture (by hash, ``None`` →
    ``initials`` on ``color``) framed in ``color`` (``None`` → muted)."""

    user_id: str
    initials: str
    color: str | None
    picture: str | None
    stale: bool


@dataclass(slots=True, frozen=True)
class SceneBubble:
    """A Map bubble (:class:`~.map_view.MapBubble`): at a zone centre
    (``zone``, ``radius_m``) or at one person's 4-dp point."""

    key: str
    lat: float
    lon: float
    radius_m: int
    zone: str | None
    home: bool
    faces: tuple[SceneFace, ...]
    icon: SceneIcon
    place: str
    names: str
    stale: bool
    age: str | None
    away: bool


@dataclass(slots=True, frozen=True)
class ScenePin:
    """A household zone: its circle, pin and name (muted when empty)."""

    zone: str
    name: str
    lat: float
    lon: float
    radius_m: int
    color: str | None
    icon: SceneIcon
    occupied: bool


@dataclass(slots=True, frozen=True)
class MapScene:
    """Everything the picture shows, in the browser's order. ``more_one``
    / ``more_other`` (``{n}``) spell a merged caption's "+1 place"."""

    bubbles: tuple[SceneBubble, ...]
    pins: tuple[ScenePin, ...]
    attribution: str
    more_one: str
    more_other: str


@dataclass(slots=True, frozen=True)
class MapPalette:
    """The board colours one scheme draws with (``dashboard.css``)."""

    bg: str
    surface: str
    text: str
    muted: str
    border: str
    accent: str
    bubble: str


#: ``dashboard.css``'s colours, should a token be missing from it.
_FALLBACK: Final = {
    "light": {
        "bg": "#f4f2ee",
        "surface": "#ffffff",
        "text": "#1f1d1a",
        "muted": "#5f5a52",
        "border": "#d6d1c8",
        "accent": "#2f6f5e",
    },
    "dark": {
        "bg": "#151412",
        "surface": "#22201d",
        "text": "#f1eee9",
        "muted": "#b3ada3",
        "border": "#3d3a35",
        "accent": "#6cc3a8",
    },
}
_BUBBLES: Final = {"light": BUBBLE_LIGHT, "dark": BUBBLE_DARK}


def map_palettes(colors: Mapping[str, Mapping[str, str]]) -> dict[str, MapPalette]:
    """The light and dark :class:`MapPalette` from the board colours
    (:func:`~.display_json.board_colors` — the household accent included)
    and the Map bubble surfaces (:data:`~.contrast.BUBBLE_LIGHT` / ``_DARK``)."""
    out = {}
    for scheme, fallback in _FALLBACK.items():
        given = colors.get(scheme, {})
        pick = {k: given.get(k) or v for k, v in fallback.items()}
        out[scheme] = MapPalette(**pick, bubble=_BUBBLES[scheme])
    return out


def _icon(text: str) -> SceneIcon:
    return SceneIcon(text=text, code=emoji_code(text))


def map_scene(view: MapView, *, t: Callable[..., str], attribution: str) -> MapScene:
    """The picture's scene for the Map tab ``view``."""
    bubbles = tuple(
        SceneBubble(
            key=b.key,
            lat=b.latitude,
            lon=b.longitude,
            radius_m=b.zone.radius_m if b.zone is not None else 0,
            zone=b.zone.id if b.zone is not None else None,
            home=b.zone is not None and b.zone.is_home,
            faces=tuple(
                SceneFace(
                    user_id=s.person.user_id,
                    initials=s.person.initials,
                    color=hex_color(s.person.color),
                    picture=s.person.picture_hash,
                    stale=s.stale,
                )
                for s in b.spots
            ),
            icon=_icon(b.spots[0].icon),
            place=b.spots[0].place,
            names=", ".join(s.person.display_name for s in b.spots),
            stale=b.stale,
            age=b.age,
            away=b.away,
        )
        for b in view.bubbles
    )
    pins = tuple(
        ScenePin(
            zone=p.zone.id,
            name=p.zone.name,
            lat=p.zone.latitude,
            lon=p.zone.longitude,
            radius_m=p.zone.radius_m,
            color=hex_color(p.zone.color),
            icon=_icon(pin_icon(p.zone)),
            occupied=p.occupied,
        )
        for p in view.pins
    )
    return MapScene(
        bubbles=bubbles,
        pins=pins,
        attribution=attribution,
        more_one=t("dashboard.map.more_places_one"),
        more_other=t("dashboard.map.more_places", n="{n}"),
    )


def scene_key(
    scene: MapScene, *, palettes: Mapping[str, MapPalette], tiles: str
) -> str:
    """A hex digest of everything that decides the picture's pixels at a
    given size: the scene, ``palettes`` (every scheme a panel may ask
    for), the tile source tag and :data:`RENDER_VERSION`."""
    body = orjson.dumps(
        {
            "v": RENDER_VERSION,
            "scene": asdict(scene),
            "palettes": {k: asdict(v) for k, v in sorted(palettes.items())},
            "tiles": tiles,
        }
    )
    return hashlib.sha256(body).hexdigest()


def scene_etag(
    key: str, w: int, h: int, scheme: str, *, missing: tuple[str, ...] = ()
) -> str:
    """The ``ETag`` of the picture of scene ``key`` at ``w`` × ``h`` in
    ``scheme`` — and, for one painted with gaps (``missing``: tiles
    ``z/x/y``, faces ``face:<id>``), which ones, so the whole picture
    replaces it later. Size and scheme are in it although an ETag is per
    URL: a panel may keep one tag across a size or scheme change."""
    text = f"{key}|{w}x{h}|{scheme}"
    if missing:
        text += "|" + ";".join(missing)
    return '"' + hashlib.sha256(text.encode()).hexdigest()[:32] + '"'

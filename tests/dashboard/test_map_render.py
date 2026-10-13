"""Tests for the wall-panel map picture (:mod:`socialhome.dashboard.map_render`).

Painted on synthetic one-colour tiles so a pixel says what was drawn.
"""

from __future__ import annotations

import io
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.boards import board_base
from socialhome.dashboard.emoji import EMOJI_DIR
from socialhome.dashboard.map_frame import project
from socialhome.dashboard.map_render import (
    drawable,
    frame_scene,
    plan_tiles,
    render_map,
    scene_emoji,
)
from socialhome.dashboard.map_scene import MapPalette, map_scene
from socialhome.dashboard.map_view import map_view
from socialhome.dashboard.presenter import home_colors
from socialhome.dashboard.render import DashboardRenderer
from socialhome.i18n import Catalog

from .factories import LEO, MAP_PEOPLE, ZONE_WORK, located, map_home

RENDERER = DashboardRenderer(
    Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
)
T = RENDERER.translator("en")
LIGHT = MapPalette(
    bg="#f4f2ee",
    surface="#ffffff",
    text="#1f1d1a",
    muted="#5f5a52",
    border="#d6d1c8",
    accent="#2f6f5e",
    bubble="#ffffff",
)
DARK = MapPalette(
    bg="#151412",
    surface="#22201d",
    text="#f1eee9",
    muted="#b3ada3",
    border="#3d3a35",
    accent="#6cc3a8",
    bubble="#3d3a35",
)
W, H = 748, 560


def _png(color: tuple[int, int, int], size: int = 256) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (size, size), color).save(out, format="PNG")
    return out.getvalue()


TILE_BYTES = _png((0, 120, 255))


def _scene(snap=None):
    snap = snap or map_home()
    base = board_base(snap, people=snap.people, colors=home_colors(snap), t=T)
    view = map_view(snap, base.fmt)
    assert view is not None
    return map_scene(view, t=T, attribution="© OpenStreetMap contributors")


def _emoji(scene) -> dict[str, bytes]:
    return {
        c: (EMOJI_DIR / f"{c}.png").read_bytes()
        for c in scene_emoji(scene)
        if (EMOJI_DIR / f"{c}.png").is_file()
    }


def _render(scene, *, w=W, h=H, tiles=None, pictures=None, palette=LIGHT, dark=False):
    if tiles is None:
        tiles = {(t.z, t.x, t.y): TILE_BYTES for t in plan_tiles(scene, w, h)}
    out = render_map(
        scene,
        w=w,
        h=h,
        palette=palette,
        dark=dark,
        tiles=tiles,
        pictures=pictures or {},
        emoji=_emoji(scene),
    )
    with Image.open(io.BytesIO(out.png)) as img:
        assert img.format == "PNG"
        return out, img.convert("RGB")


def _at(scene, key, w=W, h=H) -> tuple[int, int]:
    frame = frame_scene(scene, w, h).frame
    x0, y0 = frame.origin(w, h)
    b = next(b for b in scene.bubbles if b.key == key)
    x, y = project(b.lat, b.lon, frame.z)
    return round(x - x0), round(y - y0)


def _close(a, b, tol=12) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


@pytest.mark.parametrize(("w", "h"), [(W, H), (200, 200), (1920, 1200), (1280, 800)])
def test_png_has_the_asked_size(w, h):
    out, img = _render(_scene(), w=w, h=h)
    assert img.size == (w, h)
    assert out.missing == ()


def test_tiles_are_the_ground_and_quietened():
    _, img = _render(_scene())
    corner = img.getpixel((5, 5))
    # The tile's blue, desaturated and softened like the tablet's filter.
    assert corner != (0, 120, 255) and corner[2] > corner[0]
    assert not _close(corner, (0xF4, 0xF2, 0xEE))


def test_missing_tiles_leave_the_board_colour():
    out, img = _render(_scene(), tiles={})
    assert _close(img.getpixel((5, 5)), (0xF4, 0xF2, 0xEE), tol=1)
    scene = _scene()
    assert out.missing == tuple(
        sorted((t.z, t.x, t.y) for t in plan_tiles(scene, W, H))
    )


def test_unreadable_tile_counts_as_missing():
    scene = _scene()
    grid = plan_tiles(scene, W, H)
    tiles = {(t.z, t.x, t.y): TILE_BYTES for t in grid}
    first = grid[0]
    tiles[(first.z, first.x, first.y)] = b"<html>not a tile</html>"
    out, _ = _render(scene, tiles=tiles)
    assert out.missing == ((first.z, first.x, first.y),)


def test_dark_scheme_uses_the_dark_board():
    _, img = _render(_scene(), tiles={}, palette=DARK, dark=True)
    assert _close(img.getpixel((5, 5)), (0x15, 0x14, 0x12), tol=1)


def test_person_outside_zones_gets_a_dot_on_their_point():
    scene = _scene()
    _, img = _render(scene)
    x, y = _at(scene, "person:u-anna")
    anna = next(b for b in scene.bubbles if b.key == "person:u-anna").faces[0]
    assert anna.color is not None
    centre = img.getpixel((x, y))
    r, g, b = (int(anna.color[i : i + 2], 16) for i in (1, 3, 5))
    # The dot is filled with her (frame) colour — never the blue ground.
    assert _close(centre, (r, g, b), tol=60) and not _close(centre, (0, 120, 255), 60)
    # Her bubble sits above the dot: white bubble body there.
    assert _close(img.getpixel((x, y - 12 - 12 - 4)), (255, 255, 255), tol=4)


def test_picture_is_drawn_in_its_face():
    scene = _scene()
    red = _png((255, 0, 0), 96)
    _, img = _render(scene, pictures={"u-anna": red})
    x, y = _at(scene, "person:u-anna")
    # Face centre: bubble bottom (dot lift 12 + tail 12) − padding − half a face.
    face = img.getpixel((x, y - 12 - 12 - 8 - 28))
    assert _close(face, (255, 0, 0), tol=10)


def test_stale_faces_are_faded():
    gone = located(LEO, 600, lat=52.5, lon=13.4, presence="away")
    fresh = located(LEO, 5, lat=52.5, lon=13.4, presence="away")
    red = _png((255, 0, 0), 96)
    stale_scene, fresh_scene = (
        _scene(map_home(people=(gone,))),
        _scene(map_home(people=(fresh,))),
    )
    _, stale = _render(stale_scene, pictures={"u-leo": red})
    _, live = _render(fresh_scene, pictures={"u-leo": red})
    x, y = _at(stale_scene, "person:u-leo")
    probe = (x, y - 12 - 12 - 8 - 28)
    assert _close(live.getpixel(probe), (255, 0, 0), tol=10)
    r, g, b = stale.getpixel(probe)
    assert r - g < 80  # grey-ish and half see-through, no longer red


def test_far_person_becomes_an_edge_chip():
    people = list(MAP_PEOPLE)
    people[1] = replace(people[1], latitude=40.7128, longitude=-74.006)  # New York
    scene = _scene(map_home(people=tuple(people)))
    framing = frame_scene(scene, W, H)
    far = [scene.bubbles[i].key for i in framing.far]
    assert far == ["person:u-anna"]
    _, img = _render(scene)
    # West of Berlin: a white chip on the left edge.
    whites = [
        y for y in range(H) if _close(img.getpixel((30, y)), (255, 255, 255), tol=4)
    ]
    assert whites


def test_attribution_is_always_drawn():
    _, img = _render(_scene())
    box = img.crop((W - 120, H - 16, W - 8, H - 4))
    darkest = min(sum(px) for px in box.get_flattened_data())
    assert darkest < 3 * 0x90  # the muted text on the light surface


def test_drawable():
    assert drawable("Müller · Zoë …")
    assert not drawable("אבא")


def test_unknown_glyph_label_is_skipped_not_boxed():
    people = list(MAP_PEOPLE)
    people[1] = replace(people[1], display_name="אנה")
    scene = _scene(map_home(people=tuple(people)))
    out, img = _render(scene)
    assert img.size == (W, H) and out.missing == ()


def test_a_huge_empty_zone_ring_is_cheap():
    """The largest zone (50 km) seen at zoom 16: its dashed ring is
    thousands of dashes, nearly all outside the picture."""
    people = (located(LEO, 5, lat=52.52, lon=13.405, presence="away"),)
    zones = (replace(ZONE_WORK, radius_m=50_000, latitude=52.52, longitude=13.41),)
    scene = _scene(map_home(people=people, zones=zones))
    start = time.perf_counter()
    out, img = _render(scene)
    assert time.perf_counter() - start < 5
    assert img.size == (W, H)

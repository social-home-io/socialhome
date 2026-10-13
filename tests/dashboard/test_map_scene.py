"""Tests for the map picture's scene (:mod:`socialhome.dashboard.map_scene`)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.boards import board_base
from socialhome.dashboard.map_scene import (
    MapPalette,
    map_palettes,
    map_scene,
    scene_etag,
    scene_key,
)
from socialhome.dashboard.map_view import map_view
from socialhome.dashboard.presenter import home_colors
from socialhome.dashboard.render import DashboardRenderer
from socialhome.i18n import Catalog

from .factories import LEO, MAP_PEOPLE, at, map_home

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
PALETTES = {"light": LIGHT, "dark": replace(LIGHT, bg="#151412")}


def _scene(snap=None, attribution="© OpenStreetMap contributors"):
    snap = snap or map_home()
    base = board_base(snap, people=snap.people, colors=home_colors(snap), t=T)
    view = map_view(snap, base.fmt)
    assert view is not None
    return map_scene(view, t=T, attribution=attribution)


def test_scene_mirrors_the_map_view():
    scene = _scene()
    keys = [b.key for b in scene.bubbles]
    # Home first, then the other zones A→Z, then everyone outside a zone.
    assert keys == ["zone:hz_home", "zone:hz_school", "person:u-anna"]
    home = scene.bubbles[0]
    assert home.home and home.zone == "hz_home" and home.radius_m == 100
    assert [f.user_id for f in home.faces] == ["u-mia", "u-pascal"]
    assert home.icon.text == "🏠" and home.icon.code == "1f3e0"
    assert home.place == "Home" and home.names == "Mia, Pascal"
    anna = scene.bubbles[2]
    assert anna.zone is None and anna.radius_m == 0 and anna.away
    assert (anna.lat, anna.lon) == (52.5232, 13.3961)
    assert anna.faces[0].color is not None and anna.faces[0].color.startswith("#")
    pins = {p.zone: p for p in scene.pins}
    assert pins["hz_work"].icon.text == "W" and pins["hz_work"].icon.code is None
    assert not pins["hz_work"].occupied and pins["hz_home"].occupied
    assert scene.attribution == "© OpenStreetMap contributors"
    assert scene.more_one and "{n}" in scene.more_other


def test_stale_bubble_carries_its_age():
    gone = replace(
        LEO, located_at=at(10), presence="away", latitude=52.5, longitude=13.4
    )
    scene = _scene(map_home(people=(gone,)))
    (bubble,) = scene.bubbles
    assert bubble.stale and bubble.faces[0].stale
    assert bubble.age == "4 h ago"


def test_key_changes_with_what_is_drawn_only():
    scene = _scene()
    key = scene_key(scene, palettes=PALETTES, tiles="t1")
    assert key == scene_key(_scene(), palettes=PALETTES, tiles="t1")
    assert key != scene_key(scene, palettes=PALETTES, tiles="t2")
    dark = {**PALETTES, "dark": replace(LIGHT, accent="#000000")}
    assert key != scene_key(scene, palettes=dark, tiles="t1")
    moved = list(MAP_PEOPLE)
    moved[1] = replace(moved[1], latitude=52.5233)
    assert key != scene_key(
        _scene(map_home(people=tuple(moved))), palettes=PALETTES, tiles="t1"
    )
    new_picture = list(MAP_PEOPLE)
    new_picture[0] = replace(new_picture[0], picture_hash="new")
    assert key != scene_key(
        _scene(map_home(people=tuple(new_picture))), palettes=PALETTES, tiles="t1"
    )


def test_etag_shape():
    tag = scene_etag("ab" * 32, 748, 560, "light")
    assert len(tag) == 34 and tag.startswith('"') and tag.endswith('"')
    assert tag == scene_etag("ab" * 32, 748, 560, "light")
    # Every size and scheme is its own picture.
    assert tag != scene_etag("ab" * 32, 749, 560, "light")
    assert tag != scene_etag("ab" * 32, 748, 560, "dark")
    gap = scene_etag("ab" * 32, 748, 560, "light", missing=("15/1/2",))
    assert gap != tag and gap != scene_etag(
        "ab" * 32, 748, 560, "light", missing=("face:u-anna",)
    )


def test_palettes_from_the_board_colours():
    colors = {
        "light": {"bg": "#000001", "accent": "#123456"},
        "dark": {"bg": "#000002"},
    }
    palettes = map_palettes(colors)
    assert palettes["light"].bg == "#000001" and palettes["light"].accent == "#123456"
    assert palettes["light"].bubble == "#ffffff"
    assert palettes["dark"].bg == "#000002" and palettes["dark"].bubble == "#3d3a35"
    # A token the CSS lacks keeps the built-in colour.
    assert palettes["dark"].surface == "#22201d"
    assert map_palettes({})["light"].text == "#1f1d1a"

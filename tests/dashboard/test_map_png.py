"""Tests for the wall-panel map picture service (:mod:`socialhome.dashboard.map_png`)."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import pytest
from PIL import Image

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.avatars import AvatarImages
from socialhome.dashboard.boards import board_base
from socialhome.dashboard.emoji import EmojiImages
from socialhome.dashboard.map_png import (
    DEFAULT_SIZE,
    MAX_SIZE,
    MIN_SIZE,
    RETRY_INCOMPLETE_S,
    MapPictures,
    MapTilesUnavailable,
    clamp_size,
)
from socialhome.dashboard.map_render import plan_tiles
from socialhome.dashboard.map_scene import map_scene, scene_etag
from socialhome.dashboard.map_view import map_view
from socialhome.dashboard.presenter import home_colors
from socialhome.dashboard.render import DashboardRenderer
from socialhome.i18n import Catalog
from .factories import PictureTiles, map_home, solid_png
from .test_map_render import LIGHT

RENDERER = DashboardRenderer(
    Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
)
T = RENDERER.translator("en")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _pictures_fn(user_id: str):
    if user_id == "u-anna":
        return solid_png((255, 0, 0), 96), "hash-anna"
    return None


def _scene():
    snap = map_home()
    base = board_base(snap, people=snap.people, colors=home_colors(snap), t=T)
    view = map_view(snap, base.fmt)
    assert view is not None
    return map_scene(view, t=T, attribution="© OpenStreetMap contributors")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tiles() -> PictureTiles:
    return PictureTiles()


@pytest.fixture
def service(tiles, clock) -> MapPictures:
    return MapPictures(
        tiles=tiles,
        emoji=EmojiImages(),
        avatars=AvatarImages(),
        pictures=_pictures_fn,
        clock=clock,
    )


def test_clamp_size():
    assert clamp_size(None, None) == DEFAULT_SIZE
    assert clamp_size(10, 99999) == (MIN_SIZE[0], MAX_SIZE[1])
    assert clamp_size(800, 480) == (800, 480)


async def test_render_and_cache(service, tiles):
    scene = _scene()
    assert service.cached("k", 748, 560, "light") is None
    pic = await service.render(
        scene, "k" * 64, w=748, h=560, scheme="light", palette=LIGHT
    )
    assert pic.complete and pic.etag == scene_etag("k" * 64, 748, 560, "light")
    with Image.open(io.BytesIO(pic.png)) as img:
        assert img.size == (748, 560)
    fetched = len(tiles.calls)
    assert fetched == len(plan_tiles(scene, 748, 560))
    assert service.cached("k" * 64, 748, 560, "light") is pic
    # Another size or scheme is another picture.
    assert service.cached("k" * 64, 800, 480, "light") is None
    assert service.cached("k" * 64, 748, 560, "dark") is None
    again = await service.render(
        scene, "k" * 64, w=748, h=560, scheme="light", palette=LIGHT
    )
    assert again is pic and len(tiles.calls) == fetched


async def test_tile_fetches_are_bounded(service, tiles):
    await service.render(
        _scene(), "a" * 64, w=1920, h=1200, scheme="light", palette=LIGHT
    )
    assert 1 < tiles.peak <= 4


async def test_missing_tiles_render_and_retry_later(service, tiles, clock, caplog):
    scene = _scene()
    grid = plan_tiles(scene, 748, 560)
    tiles.fail = {(grid[0].z, grid[0].x, grid[0].y)}
    with caplog.at_level(logging.WARNING, logger="socialhome.dashboard.map_png"):
        pic = await service.render(
            scene, "b" * 64, w=748, h=560, scheme="light", palette=LIGHT
        )
        await service.render(
            scene, "c" * 64, w=748, h=560, scheme="light", palette=LIGHT
        )
    assert not pic.complete
    z, x, y = next(iter(tiles.fail))
    assert pic.etag == scene_etag(
        "b" * 64, 748, 560, "light", missing=(f"{z}/{x}/{y}",)
    )
    # Logged once, not once per picture.
    assert len([r for r in caplog.records if "tiles" in r.getMessage()]) == 1
    assert service.cached("b" * 64, 748, 560, "light") is pic
    clock.now += RETRY_INCOMPLETE_S
    assert service.cached("b" * 64, 748, 560, "light") is None
    tiles.fail = set()
    whole = await service.render(
        scene, "b" * 64, w=748, h=560, scheme="light", palette=LIGHT
    )
    assert whole.complete and whole.etag == scene_etag("b" * 64, 748, 560, "light")


async def test_no_tile_at_all_is_unavailable_and_remembered(service, tiles, clock):
    tiles.fail = None
    with pytest.raises(MapTilesUnavailable):
        await service.render(
            _scene(), "d" * 64, w=748, h=560, scheme="light", palette=LIGHT
        )
    asked = len(tiles.calls)
    # A minute without asking the dead upstream again for this picture.
    with pytest.raises(MapTilesUnavailable):
        await service.render(
            _scene(), "d" * 64, w=748, h=560, scheme="light", palette=LIGHT
        )
    assert len(tiles.calls) == asked
    clock.now += RETRY_INCOMPLETE_S
    tiles.fail = set()
    pic = await service.render(
        _scene(), "d" * 64, w=748, h=560, scheme="light", palette=LIGHT
    )
    assert pic.complete


async def test_slow_tiles_are_left_out_after_the_deadline(service, tiles, monkeypatch):
    monkeypatch.setattr("socialhome.dashboard.map_png.TILE_DEADLINE_S", 0.05)
    scene = _scene()
    slow = plan_tiles(scene, 748, 560)[0]
    real = tiles.fetch

    async def fetch(z, x, y):
        if (z, x, y) == (slow.z, slow.x, slow.y):
            await asyncio.sleep(30)
        return await real(z, x, y)

    monkeypatch.setattr(tiles, "fetch", fetch)
    pic = await service.render(
        scene, "f" * 64, w=748, h=560, scheme="light", palette=LIGHT
    )
    assert not pic.complete


async def test_unreadable_face_is_painted_again_later(tiles, clock):
    async def no_picture(user_id):
        return None

    service = MapPictures(
        tiles=tiles,
        emoji=EmojiImages(),
        avatars=AvatarImages(),
        pictures=no_picture,
        clock=clock,
    )
    pic = await service.render(
        _scene(), "g" * 64, w=748, h=560, scheme="light", palette=LIGHT
    )
    # Anna has a picture the read model couldn't hand over: initials now,
    # the picture again after a while.
    assert not pic.complete
    assert pic.etag == scene_etag("g" * 64, 748, 560, "light", missing=("face:u-anna",))


async def test_pictures_and_emoji_are_read_for_the_faces(service):
    pic = await service.render(
        _scene(), "e" * 64, w=748, h=560, scheme="light", palette=LIGHT
    )
    assert pic.complete


async def test_cache_is_bounded(service):
    scene = _scene()
    for i in range(12):
        await service.render(
            scene, f"{i:064d}", w=200, h=200, scheme="light", palette=LIGHT
        )
    assert service.cache_entries <= 8
    assert service.cached(f"{0:064d}", 200, 200, "light") is None
    assert service.cached(f"{11:064d}", 200, 200, "light") is not None

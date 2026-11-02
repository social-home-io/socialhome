"""Tests for :mod:`socialhome.dashboard.photo_png` — the screensaver's
panel-sized PNGs, drawn from the stored WebP and kept in a bounded LRU."""

from __future__ import annotations

import io
import time

import pytest
from PIL import Image

from socialhome.dashboard.photo_png import (
    DEFAULT_SIZE,
    MAX_SIZE,
    MIN_SIZE,
    PhotoPictures,
    clamp_size,
    fitted_size,
    panel_png,
)

from .factories import webp


def _size_mode(png: bytes) -> tuple[tuple[int, int], str, str]:
    with Image.open(io.BytesIO(png)) as img:
        return img.size, img.mode, img.format or ""


@pytest.mark.parametrize(
    ("w", "h", "expected"),
    [
        (None, None, DEFAULT_SIZE),
        (1024, 600, (1024, 600)),
        (10, 10, MIN_SIZE),
        (99999, 99999, MAX_SIZE),
    ],
)
def test_clamp_size(w, h, expected):
    assert clamp_size(w, h) == expected


@pytest.mark.parametrize(
    ("src", "box", "expected"),
    [
        ((2560, 1920), (1280, 800), (1067, 800)),  # 4:3 landscape
        ((1920, 2560), (1280, 800), (600, 800)),  # portrait
        ((2560, 1440), (1280, 800), (1280, 720)),  # 16:9
        ((640, 480), (1280, 800), (640, 480)),  # never enlarged
        ((0, 10), (1280, 800), (1, 1)),
    ],
)
def test_fitted_size(src, box, expected):
    assert fitted_size(*src, *box) == expected


def test_panel_png_fits_an_opaque_rgb_png():
    body = webp((10, 200, 30), (2560, 1920))
    size, mode, fmt = _size_mode(panel_png(body, 1280, 800))
    assert (size, mode, fmt) == ((1067, 800), "RGB", "PNG")


def test_panel_png_flattens_transparency():
    out = io.BytesIO()
    Image.new("RGBA", (400, 300), (255, 0, 0, 0)).save(out, format="WEBP")
    size, mode, _ = _size_mode(panel_png(out.getvalue(), 1280, 800))
    assert (size, mode) == ((400, 300), "RGB")


def test_panel_png_refuses_what_is_no_picture():
    with pytest.raises(Exception):  # noqa: B017 — Pillow's own error
        panel_png(b"not a picture", 1280, 800)


def test_panel_png_size_and_time_for_a_full_photo():
    """A 2560 × 1920 stored photo (noise: the worst case for PNG) drawn
    for the 1280 × 800 panel — the size the panel downloads, the time a
    worker thread spends (logged for the PR, generously bounded here)."""
    noise = Image.effect_noise((2560, 1920), 64).convert("RGB")
    out = io.BytesIO()
    noise.save(out, format="WEBP", quality=82)
    started = time.perf_counter()
    png = panel_png(out.getvalue(), 1280, 800)
    took = time.perf_counter() - started
    assert _size_mode(png)[0] == (1067, 800)
    assert len(png) < 1067 * 800 * 3 + 64 * 1024
    assert took < 10


async def test_pictures_are_kept_and_bounded():
    pictures = PhotoPictures(max_entries=2)
    body = webp((1, 2, 3), (300, 200))
    assert pictures.cached("a", "a.webp", 1280, 800) is None
    first = await pictures.render("a", "a.webp", body, 1280, 800)
    assert pictures.cached("a", "a.webp", 1280, 800) == first
    # Another file name (re-processed photo) or size is another picture.
    assert pictures.cached("a", "b.webp", 1280, 800) is None
    assert pictures.cached("a", "a.webp", 1024, 600) is None
    await pictures.render("b", "b.webp", body, 1280, 800)
    pictures.cached("a", "a.webp", 1280, 800)  # a is now the newest
    await pictures.render("c", "c.webp", body, 1280, 800)
    assert pictures.cached("b", "b.webp", 1280, 800) is None
    assert pictures.cached("a", "a.webp", 1280, 800) == first


async def test_pictures_are_bounded_by_bytes():
    body = webp((1, 2, 3), (300, 200))
    one = len(panel_png(body, 1280, 800))
    pictures = PhotoPictures(max_entries=10, max_bytes=one * 2)
    for name in ("a", "b", "c"):
        await pictures.render(name, f"{name}.webp", body, 1280, 800)
    assert pictures.cached("a", "a.webp", 1280, 800) is None
    assert pictures.cached("c", "c.webp", 1280, 800) is not None
    # Drawing the same one again replaces it (no double count).
    await pictures.render("c", "c.webp", body, 1280, 800)
    assert pictures.cached("b", "b.webp", 1280, 800) is not None

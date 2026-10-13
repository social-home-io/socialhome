"""Tests for wall-panel avatars (:mod:`socialhome.dashboard.avatars`)."""

from __future__ import annotations

import io

import pytest
from PIL import Image, UnidentifiedImageError

from socialhome.dashboard.avatars import (
    AVATAR_SIZES,
    DEFAULT_AVATAR_SIZE,
    AvatarImages,
    square_png,
)


def _webp(w: int = 120, h: int = 80) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(out, format="WEBP")
    return out.getvalue()


def test_sizes():
    assert DEFAULT_AVATAR_SIZE in AVATAR_SIZES


def test_square_png_crops_and_scales_webp():
    body = square_png(_webp(), 64)
    with Image.open(io.BytesIO(body)) as img:
        assert img.format == "PNG" and img.size == (64, 64) and img.mode == "RGBA"


async def test_cache_is_keyed_by_tag_and_size_and_bounded():
    images = AvatarImages(max_entries=2)
    first = await images.png(_webp(), "pic1", 48)
    # Same tag and size: the cached bytes, whatever body is passed.
    assert await images.png(b"not an image", "pic1", 48) == first
    other = await images.png(_webp(50, 50), "pic1", 96)
    assert other != first
    await images.png(_webp(), "pic2", 48)
    # pic1@48 was the least recently used: evicted, so it is decoded again.
    with pytest.raises(UnidentifiedImageError):
        await images.png(b"not an image", "pic1", 48)

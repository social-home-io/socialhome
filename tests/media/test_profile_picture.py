"""Tests for socialhome.media.profile_picture."""

from __future__ import annotations

import base64
import io
import struct
import zlib

import pytest
from PIL import Image

from socialhome.domain.errors import ImageUnreadableError
from socialhome.domain.picture_hash import compute_picture_hash
from socialhome.media.profile_picture import (
    PROFILE_PICTURE_MAX_DIMENSION,
    PROFILE_PICTURE_MAX_SOURCE_PIXELS,
    normalize_profile_picture,
    reencode_peer_picture,
)


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="purple").save(buf, format="PNG")
    return buf.getvalue()


async def test_normalize_reencodes_to_a_bounded_webp_with_our_hash():
    raw = _png(800, 800)
    webp, hash_ = await normalize_profile_picture(raw)
    assert webp != raw
    img = Image.open(io.BytesIO(webp))
    assert img.format == "WEBP"
    assert img.size == (PROFILE_PICTURE_MAX_DIMENSION, PROFILE_PICTURE_MAX_DIMENSION)
    assert hash_ == compute_picture_hash(webp)


async def test_normalize_refuses_bytes_that_are_not_an_image():
    with pytest.raises(ValueError):
        await normalize_profile_picture(b"<svg><script>alert(1)</script></svg>")


async def test_reencode_peer_picture_decodes_base64_first():
    raw = _png(40, 20)
    webp, hash_ = await reencode_peer_picture(base64.b64encode(raw).decode("ascii"))
    assert Image.open(io.BytesIO(webp)).format == "WEBP"
    assert hash_ == compute_picture_hash(webp)


@pytest.mark.parametrize(
    "wire",
    [
        "not base64 at all!",
        base64.b64encode(b"<html>hi</html>").decode("ascii"),
        12345,
        None,
        "",
    ],
)
async def test_reencode_peer_picture_refuses_unusable_wire_values(wire):
    with pytest.raises(ValueError):
        await reencode_peer_picture(wire)


def _png_header_only(width: int, height: int) -> bytes:
    """A tiny PNG whose IHDR declares ``width`` x ``height`` — a
    decompression bomb in shape: a few bytes on the wire, gigabytes once
    decoded."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    )


async def test_normalize_refuses_a_source_over_the_pixel_cap():
    side = int(PROFILE_PICTURE_MAX_SOURCE_PIXELS**0.5) + 1
    raw = _png_header_only(side, side)
    assert len(raw) < 64
    with pytest.raises(ImageUnreadableError):
        await normalize_profile_picture(raw)


async def test_normalize_refuses_a_truncated_image():
    raw = _png(500, 500)[:200]
    with pytest.raises(ImageUnreadableError):
        await normalize_profile_picture(raw)

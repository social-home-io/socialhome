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
from socialhome.domain.space_author_profile import (
    PROFILE_PICTURE_MAX_BYTES,
    PROFILE_PICTURE_MAX_PX,
)
from socialhome.media import profile_picture as profile_picture_mod
from socialhome.media.profile_picture import (
    PROFILE_PICTURE_MAX_DIMENSION,
    PROFILE_PICTURE_MAX_SOURCE_PIXELS,
    PublicPictureCache,
    normalize_profile_picture,
    public_profile_picture,
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


async def test_public_profile_picture_is_a_small_re_encoded_webp():

    buf = io.BytesIO()
    Image.new("RGB", (900, 600), (10, 120, 200)).save(buf, "PNG")
    out = await public_profile_picture(buf.getvalue())
    assert out is not None and out[:4] == b"RIFF" and out[8:12] == b"WEBP"
    assert len(out) <= PROFILE_PICTURE_MAX_BYTES
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) <= PROFILE_PICTURE_MAX_PX


async def test_public_profile_picture_never_keeps_the_input_bytes():
    """A WebP with trailing junk (a polyglot shape) comes back re-encoded."""
    buf = io.BytesIO()

    Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, "WEBP")
    polyglot = buf.getvalue() + b"<script>alert(1)</script>"
    out = await public_profile_picture(polyglot)
    assert out is None or (out != polyglot and b"<script>" not in out)


async def test_public_profile_picture_refuses_what_is_not_an_image():
    assert await public_profile_picture(b"<svg onload=alert(1)/>") is None


async def test_the_public_picture_cache_re_encodes_a_source_once(monkeypatch):
    """M4: the public re-encode (up to three decode + encode passes) runs
    once per source picture, not on every view or publish."""
    calls: list[bytes] = []
    real = profile_picture_mod.public_profile_picture

    async def _counting(raw: bytes) -> bytes | None:
        calls.append(raw)
        return await real(raw)

    monkeypatch.setattr(profile_picture_mod, "public_profile_picture", _counting)
    cache = PublicPictureCache(max_entries=2)
    src = _png(300, 300)
    first = await cache.get("aaaaaaaaaaaaaaaa", src)
    again = await cache.get("aaaaaaaaaaaaaaaa", src)
    assert first is not None and again == first
    assert len(calls) == 1


async def test_the_public_picture_cache_is_bounded_and_remembers_misses(monkeypatch):
    calls: list[bytes] = []

    async def _fake(raw: bytes) -> bytes | None:
        calls.append(raw)
        return None if raw == b"bad" else b"RIFF" + raw

    monkeypatch.setattr(profile_picture_mod, "public_profile_picture", _fake)
    cache = PublicPictureCache(max_entries=2)
    assert await cache.get("h-bad", b"bad") is None
    assert await cache.get("h-bad", b"bad") is None  # a miss is cached too
    await cache.get("h-1", b"1")
    await cache.get("h-2", b"2")  # evicts h-bad (least recently used)
    await cache.get("h-bad", b"bad")
    assert calls == [b"bad", b"1", b"2", b"bad"]
    assert len(cache) == 2

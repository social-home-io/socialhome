"""Tests for socialhome.domain.media_constraints."""

from __future__ import annotations

import base64

import pytest

from socialhome.domain.media_constraints import (
    CAPTION_MAX,
    GFS_SPACE_MEDIA_PREFIX,
    IMAGE_ACCEPTED_MIMES,
    IMAGE_MAX_DIMENSION,
    IMAGE_MAX_UPLOAD_BYTES,
    IMAGE_OUTPUT_MIME,
    IMAGE_WEBP_QUALITY,
    SPACE_COVER_BOOTSTRAP_MAX_BYTES,
    SPACE_COVER_SNAPSHOT_MAX_BYTES,
    SPACE_ICON_BOOTSTRAP_MAX_BYTES,
    SPACE_ICON_SNAPSHOT_MAX_BYTES,
    SPACE_IMAGE_DATA_URI_MAX_CHARS,
    SPACE_IMAGE_DATA_URI_PREFIX,
    SPACE_IMAGE_EMBED_MAX_BYTES,
    SPACE_IMAGE_FIT_MIN_DIMENSION,
    THUMBNAIL_PX,
    THUMBNAIL_WEBP_QUALITY,
    VIDEO_ACCEPTED_MIMES,
    VIDEO_AUDIO_BITRATE_KBPS,
    VIDEO_CRF,
    VIDEO_MAX_DIMENSION,
    VIDEO_MAX_DURATION_SECONDS,
    VIDEO_MAX_UPLOAD_BYTES,
    VIDEO_OUTPUT_MIME,
    is_gfs_space_media_name,
    is_reserved_media_name,
    is_safe_media_name,
    local_media_ref,
    space_media_ref,
)


def test_image_constants_sensible():
    """Image protocol constants have reasonable values."""
    # 2560 px = 1:1 fit for the short edge of an iPad Pro 12.9 /
    # 14"-16" retina MacBook so full-bleed photos aren't upscaled.
    assert IMAGE_MAX_DIMENSION == 2560
    assert 1 <= IMAGE_WEBP_QUALITY <= 100
    assert IMAGE_MAX_UPLOAD_BYTES > 0
    assert IMAGE_OUTPUT_MIME == "image/webp"
    assert "image/jpeg" in IMAGE_ACCEPTED_MIMES
    assert "image/webp" in IMAGE_ACCEPTED_MIMES


def test_video_constants_sensible():
    """Video protocol constants have reasonable values."""
    # 1920 px = true 1080p — what flagship phones capture by default.
    assert VIDEO_MAX_DIMENSION == 1920
    # CRF is a libvpx/libx264 "constant rate factor": 0 is lossless,
    # ~50 is terrible. 24-25 is the industry sweet spot for "visually
    # transparent" on consumer content; 20-28 is the useful range.
    assert 20 <= VIDEO_CRF <= 28
    assert VIDEO_MAX_DURATION_SECONDS == 60
    # Opus bitrate — 64 kbps is speech-only, 128+ is overkill. Land
    # somewhere that's transparent for both speech and music.
    assert 64 <= VIDEO_AUDIO_BITRATE_KBPS <= 128
    assert VIDEO_MAX_UPLOAD_BYTES > IMAGE_MAX_UPLOAD_BYTES
    assert VIDEO_OUTPUT_MIME == "video/webm"
    assert "video/mp4" in VIDEO_ACCEPTED_MIMES


def test_shared_constants():
    """Thumbnail and caption limits are set."""
    # 512 px keeps tablet/desktop grid thumbs sharp at 3× retina
    # (~170 dp wide). 400 px was the phone-grid baseline.
    assert THUMBNAIL_PX == 512
    # Thumbnails are smaller than the main image so they deserve
    # lower quality; any higher and the constant is redundant.
    assert 1 <= THUMBNAIL_WEBP_QUALITY < IMAGE_WEBP_QUALITY
    assert CAPTION_MAX == 300


def test_space_image_embed_bound():
    """The data-URI bound is exactly the prefix + base64 of the byte bound,
    so the GFS stores precisely what a household embeds and nothing more."""
    uri = SPACE_IMAGE_DATA_URI_PREFIX + base64.b64encode(
        b"\0" * SPACE_IMAGE_EMBED_MAX_BYTES
    ).decode("ascii")
    assert len(uri) == SPACE_IMAGE_DATA_URI_MAX_CHARS
    # Far above a real dimension-capped cover, yet bounded.
    assert 512 * 1024 < SPACE_IMAGE_EMBED_MAX_BYTES <= 2 * 1024 * 1024


def test_space_snapshot_image_bounds():
    """Invite-link (relay) bounds are tighter than the peer-envelope ones,
    which are tighter than the GFS publish bound; each icon bound is under
    its cover bound. Both §D2b images base64 to well under half the 256 KiB
    sealed-blob cap, leaving the rest for config + roster."""
    assert SPACE_ICON_BOOTSTRAP_MAX_BYTES < SPACE_COVER_BOOTSTRAP_MAX_BYTES
    assert SPACE_ICON_SNAPSHOT_MAX_BYTES < SPACE_COVER_SNAPSHOT_MAX_BYTES
    assert SPACE_COVER_BOOTSTRAP_MAX_BYTES < SPACE_COVER_SNAPSHOT_MAX_BYTES
    assert SPACE_COVER_SNAPSHOT_MAX_BYTES < SPACE_IMAGE_EMBED_MAX_BYTES
    both_b64 = 4 * -(
        -(SPACE_COVER_BOOTSTRAP_MAX_BYTES + SPACE_ICON_BOOTSTRAP_MAX_BYTES) // 3
    )
    assert both_b64 < 128 * 1024
    assert 16 <= SPACE_IMAGE_FIT_MIN_DIMENSION <= 256


# ─── Local media references (moved from inbound_media_store) ─────────


@pytest.mark.parametrize(
    "name",
    ["0f3a9c.webp", "a1b2-c3d4", "m-1.part00001", "blob:instance", "x_y.webm"],
)
def test_safe_names_accepted(name):
    assert is_safe_media_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "../x", "a/b", "a\\b", ".hidden", "..", "a\x00b", "a b", "a" * 201, "é.webp"],
)
def test_unsafe_names_rejected(name):
    assert not is_safe_media_name(name)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("api/media/0f3a9c.webp", "api/media/0f3a9c.webp"),
        ("/api/media/0f3a9c.webp", "api/media/0f3a9c.webp"),
        ("api/media/0f3a9c.webp?exp=1&sig=x", "api/media/0f3a9c.webp"),
        ("0f3a9c.webp", None),
        ("https://example.org/api/media/x.webp", None),
        ("api/media/../secret", None),
        ("api/media/a/b.webp", None),
        ("api/media/.hidden", None),
        ("api/media/", None),
        ("api/other/x.webp", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_local_media_ref(value, expected):
    assert local_media_ref(value) == expected


@pytest.mark.parametrize(
    ("name", "reserved"),
    [
        ("dm-" + "0" * 40 + ".webp", True),
        ("DM-x.webp", True),
        ("gm-" + "a" * 40 + ".webm", True),
        ("m-1.preview.webp", True),
        ("m-1.part0003", True),
        ("x.assembled", True),
        ("0123abcd" * 4 + ".webp", False),
        ("a1.webp", False),
        ("admin.webp", False),
    ],
)
def test_is_reserved_media_name(name, reserved):
    assert is_reserved_media_name(name) is reserved


def test_space_media_ref_drops_reserved_names():
    """A space item (post, cover, gallery item) received from another
    household never names a file the DM or moment-media paths own: such a
    reference would show — and, through the GFS cache, re-publish — a file
    that belongs to someone else."""
    assert space_media_ref("api/media/a.webp") == "api/media/a.webp"
    assert space_media_ref("/api/media/a.webp?sig=x") == "api/media/a.webp"
    assert space_media_ref("api/media/gm-abc.webp") is None
    assert space_media_ref("api/media/dm-abc.webp") is None
    assert space_media_ref("https://evil.example/a.webp") is None


def test_a_gfs_reader_copy_name():
    name = GFS_SPACE_MEDIA_PREFIX + "ab" * 20 + ".webp"
    assert is_gfs_space_media_name(name)
    assert not is_gfs_space_media_name("ab.webp")
    assert not is_gfs_space_media_name(GFS_SPACE_MEDIA_PREFIX + "../x")

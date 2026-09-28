"""Tests for socialhome.domain.media_constraints."""

from __future__ import annotations

import base64

from socialhome.domain.media_constraints import (
    SPACE_IMAGE_DATA_URI_MAX_CHARS,
    SPACE_IMAGE_DATA_URI_PREFIX,
    SPACE_IMAGE_EMBED_MAX_BYTES,
    CAPTION_MAX,
    IMAGE_ACCEPTED_MIMES,
    IMAGE_MAX_DIMENSION,
    IMAGE_MAX_UPLOAD_BYTES,
    IMAGE_OUTPUT_MIME,
    IMAGE_WEBP_QUALITY,
    THUMBNAIL_PX,
    THUMBNAIL_WEBP_QUALITY,
    VIDEO_ACCEPTED_MIMES,
    VIDEO_AUDIO_BITRATE_KBPS,
    VIDEO_CRF,
    VIDEO_MAX_DIMENSION,
    VIDEO_MAX_DURATION_SECONDS,
    VIDEO_MAX_UPLOAD_BYTES,
    VIDEO_OUTPUT_MIME,
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

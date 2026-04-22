"""Tests for socialhome.domain.media_constraints."""

from __future__ import annotations

from socialhome.domain.media_constraints import (
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
    assert IMAGE_MAX_DIMENSION == 2048
    assert 1 <= IMAGE_WEBP_QUALITY <= 100
    assert IMAGE_MAX_UPLOAD_BYTES > 0
    assert IMAGE_OUTPUT_MIME == "image/webp"
    assert "image/jpeg" in IMAGE_ACCEPTED_MIMES
    assert "image/webp" in IMAGE_ACCEPTED_MIMES


def test_video_constants_sensible():
    """Video protocol constants have reasonable values."""
    assert VIDEO_MAX_DIMENSION == 1280
    # CRF is a libvpx/libx264 "constant rate factor": 0 is lossless,
    # ~50 is terrible. 20-32 is the useful range; we want aggressive
    # compression for household clips.
    assert 20 <= VIDEO_CRF <= 32
    assert VIDEO_MAX_DURATION_SECONDS == 60
    # Opus bitrate — 64 kbps is speech-only, 128+ is overkill. Land
    # somewhere that's transparent for both speech and music.
    assert 64 <= VIDEO_AUDIO_BITRATE_KBPS <= 128
    assert VIDEO_MAX_UPLOAD_BYTES > IMAGE_MAX_UPLOAD_BYTES
    assert VIDEO_OUTPUT_MIME == "video/webm"
    assert "video/mp4" in VIDEO_ACCEPTED_MIMES


def test_shared_constants():
    """Thumbnail and caption limits are set."""
    assert THUMBNAIL_PX == 400
    # Thumbnails are rendered ≤ 400 px so they deserve lower quality
    # than the main image; any higher and the constant is redundant.
    assert 1 <= THUMBNAIL_WEBP_QUALITY < IMAGE_WEBP_QUALITY
    assert CAPTION_MAX == 300

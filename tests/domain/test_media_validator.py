"""Tests for socialhome.domain.media_validator."""

from __future__ import annotations

import pytest

from socialhome.domain.media_constraints import (
    IMAGE_MAX_UPLOAD_BYTES,
    IMAGE_OUTPUT_MIME,
    VIDEO_MAX_UPLOAD_BYTES,
    VIDEO_OUTPUT_MIME,
)
from socialhome.domain.media_validator import validate_inbound_media_meta


def test_valid_image_meta():
    """Conforming image/webp file_meta passes validation."""
    validate_inbound_media_meta(
        {
            "mime_type": IMAGE_OUTPUT_MIME,
            "size_bytes": 1024,
        }
    )


def test_valid_video_meta():
    """Conforming video/webm file_meta passes validation."""
    validate_inbound_media_meta(
        {
            "mime_type": VIDEO_OUTPUT_MIME,
            "size_bytes": 1024,
        }
    )


def test_image_too_large():
    """Oversized image raises ValueError."""
    with pytest.raises(ValueError, match="protocol max"):
        validate_inbound_media_meta(
            {
                "mime_type": IMAGE_OUTPUT_MIME,
                "size_bytes": IMAGE_MAX_UPLOAD_BYTES + 1,
            }
        )


def test_video_too_large():
    """Oversized video raises ValueError."""
    with pytest.raises(ValueError, match="protocol max"):
        validate_inbound_media_meta(
            {
                "mime_type": VIDEO_OUTPUT_MIME,
                "size_bytes": VIDEO_MAX_UPLOAD_BYTES + 1,
            }
        )


def test_unknown_mime_type():
    """Non-conforming MIME type raises ValueError."""
    with pytest.raises(ValueError, match="non-conforming MIME"):
        validate_inbound_media_meta(
            {
                "mime_type": "image/png",
                "size_bytes": 100,
            }
        )


def test_missing_mime_type():
    """Missing mime_type raises ValueError."""
    with pytest.raises(ValueError, match="non-conforming MIME"):
        validate_inbound_media_meta({"size_bytes": 100})


def test_zero_size_image_ok():
    """Zero-byte image passes (edge case — technically valid)."""
    validate_inbound_media_meta(
        {
            "mime_type": IMAGE_OUTPUT_MIME,
            "size_bytes": 0,
        }
    )


def test_exact_limit_image_ok():
    """Image at exactly the protocol max passes."""
    validate_inbound_media_meta(
        {
            "mime_type": IMAGE_OUTPUT_MIME,
            "size_bytes": IMAGE_MAX_UPLOAD_BYTES,
        }
    )


def test_exact_limit_video_ok():
    """Video at exactly the protocol max passes."""
    validate_inbound_media_meta(
        {
            "mime_type": VIDEO_OUTPUT_MIME,
            "size_bytes": VIDEO_MAX_UPLOAD_BYTES,
        }
    )

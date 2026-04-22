"""Protocol-level media constraints (§5.2).

These values are **not** operator-configurable — they are part of the Social
Home wire protocol.  Every instance in the federation must agree on the same
limits so that media exchanged between instances is always accepted.

Local upload processing (image_processor, video_processor) uses these
constants to produce conformant output.  Inbound federation validation
(media_validator) uses them to reject non-conforming payloads.
"""

from __future__ import annotations


# ─── Image constraints ──────────────────────────────────────────────────────

IMAGE_MAX_DIMENSION: int = 2048
IMAGE_WEBP_QUALITY: int = 78
IMAGE_MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024  # 20 MiB
IMAGE_ACCEPTED_MIMES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
    }
)
IMAGE_OUTPUT_MIME: str = "image/webp"
IMAGE_WEBP_MAGIC: bytes = b"RIFF"
IMAGE_WEBP_MAGIC_8: bytes = b"WEBP"

# ─── Video constraints ──────────────────────────────────────────────────────

VIDEO_MAX_DIMENSION: int = 1280
VIDEO_CRF: int = 28
VIDEO_MAX_DURATION_SECONDS: int = 60
VIDEO_AUDIO_BITRATE_KBPS: int = 96
VIDEO_MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024  # 200 MiB
VIDEO_ACCEPTED_MIMES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)
VIDEO_OUTPUT_MIME: str = "video/webm"
VIDEO_WEBM_MAGIC: bytes = b"\x1a\x45\xdf\xa3"

# ─── Shared ─────────────────────────────────────────────────────────────────

THUMBNAIL_PX: int = 400
THUMBNAIL_WEBP_QUALITY: int = 75
CAPTION_MAX: int = 300

# ─── Profile / cover uploads ───────────────────────────────────────────────
#: Avatars and space cover images both ride through ImageProcessor, which
#: transcodes to WebP and caps the longest side — so the raw upload cap is
#: about accommodating high-resolution phone photos (HEIC/JPEG) rather than
#: about the on-disk size. One shared constant keeps the three upload sites
#: (profile picture, per-space member picture, space cover) in lockstep.
PROFILE_PICTURE_MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MiB

#: Space cover image resized to this longest side; larger than the 256-px
#: profile-picture cap so a hero banner has real estate.
SPACE_COVER_MAX_DIMENSION: int = 1200

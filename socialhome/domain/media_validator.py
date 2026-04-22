"""Inbound federation media validation (§5.2).

Validates ``file_meta`` dictionaries received in decrypted federation
payloads.  Only the canonical output formats (``image/webp``,
``video/webm``) are accepted — a peer instance that sends anything else
is non-conforming.
"""

from __future__ import annotations

from .media_constraints import (
    IMAGE_MAX_UPLOAD_BYTES,
    IMAGE_OUTPUT_MIME,
    VIDEO_MAX_UPLOAD_BYTES,
    VIDEO_OUTPUT_MIME,
)


def validate_inbound_media_meta(file_meta: dict) -> None:
    """Validate *file_meta* from a decrypted federation payload.

    Raises :class:`ValueError` if:

    * ``mime_type`` is not in ``{image/webp, video/webm}``.
    * ``size_bytes`` exceeds the protocol maximum for the MIME type.
    """
    mime = file_meta.get("mime_type", "")
    size = int(file_meta.get("size_bytes", 0))

    if mime == IMAGE_OUTPUT_MIME:
        if size > IMAGE_MAX_UPLOAD_BYTES:
            raise ValueError(
                f"Inbound image exceeds protocol max ({size} > {IMAGE_MAX_UPLOAD_BYTES})"
            )
    elif mime == VIDEO_OUTPUT_MIME:
        if size > VIDEO_MAX_UPLOAD_BYTES:
            raise ValueError(
                f"Inbound video exceeds protocol max ({size} > {VIDEO_MAX_UPLOAD_BYTES})"
            )
    else:
        raise ValueError(f"Inbound media has non-conforming MIME type: {mime!r}")

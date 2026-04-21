"""Image processing module — Pillow-based normalisation (spec §5.2 line 4634).

Accepts JPEG, PNG, GIF, WebP, and HEIC images. Validates via magic bytes,
auto-orients via EXIF, resizes the longest side to the protocol-defined
maximum, and converts to WebP at the protocol-defined quality.
"""

from __future__ import annotations

import io
import logging
import uuid

from PIL import Image, ImageOps
from PIL.Image import Resampling

from ..domain.media_constraints import (
    IMAGE_ACCEPTED_MIMES,
    IMAGE_MAX_DIMENSION,
    IMAGE_WEBP_QUALITY,
    THUMBNAIL_PX,
    THUMBNAIL_WEBP_QUALITY,
)

log = logging.getLogger(__name__)

# MIME type → tuple of (offset, magic_bytes)
MAGIC_BYTES: dict[str, tuple[int, bytes]] = {
    "image/jpeg": (0, b"\xff\xd8\xff"),
    "image/png": (0, b"\x89PNG\r\n\x1a\n"),
    "image/gif": (0, b"GIF8"),
    "image/webp": (8, b"WEBP"),
    "image/heic": (4, b"ftyp"),
}


class ImageProcessor:
    """Normalise uploaded images to WebP.

    Processing parameters (max dimension, quality) are protocol constants
    defined in :mod:`social_home.domain.media_constraints`.
    """

    ACCEPTED_MIME_TYPES: frozenset[str] = IMAGE_ACCEPTED_MIMES

    MAGIC_BYTES = MAGIC_BYTES

    def __init__(self) -> None:
        self._max_dimension = IMAGE_MAX_DIMENSION
        self._webp_quality = IMAGE_WEBP_QUALITY

    # ── Public API ────────────────────────────────────────────────────────

    async def process(
        self,
        data: bytes,
        filename: str,
    ) -> tuple[bytes, str]:
        """Validate, orient, resize, and convert *data* to WebP.

        Parameters
        ----------
        data:
            Raw bytes of the uploaded image file.
        filename:
            Original filename (used for logging only; the returned name
            is always ``"{uuid}.webp"``).

        Returns
        -------
        tuple[bytes, str]
            ``(webp_bytes, new_filename)`` where *new_filename* is a
            UUID-based ``.webp`` name.

        Raises
        ------
        ValueError
            If the data fails magic-byte validation or Pillow cannot
            open it.
        """
        mime = self._detect_mime(data)
        if mime is None:
            raise ValueError(
                f"Unsupported image format for file {filename!r}. "
                f"Accepted types: {', '.join(sorted(self.ACCEPTED_MIME_TYPES))}"
            )

        try:
            img = Image.open(io.BytesIO(data))
        except Exception as exc:
            raise ValueError(f"Cannot open image {filename!r}: {exc}") from exc

        # Auto-orient via EXIF (handles camera rotation)
        try:
            # exif_transpose returns a new (possibly sRGB-reencoded) Image.
            img = ImageOps.exif_transpose(img)  # type: ignore[assignment]
        except Exception:
            # Non-fatal — some images lack EXIF; proceed without.
            pass

        # Resize longest side to max_dimension preserving aspect ratio
        img = self._resize(img, self._max_dimension)

        # Convert palette/RGBA-mode images so WebP encoder is happy
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")

        out = io.BytesIO()
        img.save(out, format="WEBP", quality=self._webp_quality)
        webp_bytes = out.getvalue()
        new_filename = f"{uuid.uuid4().hex}.webp"
        return webp_bytes, new_filename

    async def generate_thumbnail(
        self,
        data: bytes,
        size: int = THUMBNAIL_PX,
    ) -> bytes:
        """Return a square-bounded WebP thumbnail of *data*.

        The image is proportionally resized so neither dimension exceeds
        *size* and encoded at :data:`THUMBNAIL_WEBP_QUALITY` — thumbnails
        render at ≤ 400 px, so the lower quality vs the main image is
        perceptually invisible and saves bytes.

        Raises
        ------
        ValueError
            If Pillow cannot open *data*.
        """
        try:
            img = Image.open(io.BytesIO(data))
        except Exception as exc:
            raise ValueError(f"Cannot open image for thumbnail: {exc}") from exc

        try:
            # exif_transpose returns a new (possibly sRGB-reencoded) Image.
            img = ImageOps.exif_transpose(img)  # type: ignore[assignment]
        except Exception:
            pass

        img = self._resize(img, size)

        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")

        out = io.BytesIO()
        img.save(out, format="WEBP", quality=THUMBNAIL_WEBP_QUALITY)
        return out.getvalue()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _detect_mime(self, data: bytes) -> str | None:
        """Return the MIME type detected from magic bytes, or ``None``."""
        for mime, (offset, magic) in MAGIC_BYTES.items():
            end = offset + len(magic)
            if len(data) >= end and data[offset:end] == magic:
                return mime
        return None

    @staticmethod
    def _resize(img, max_dim: int):
        """Proportionally resize *img* so its longest side is ≤ *max_dim*."""
        w, h = img.size
        if w <= max_dim and h <= max_dim:
            return img
        if w >= h:
            new_w = max_dim
            new_h = max(1, round(h * max_dim / w))
        else:
            new_h = max_dim
            new_w = max(1, round(w * max_dim / h))
        return img.resize((new_w, new_h), Resampling.LANCZOS)

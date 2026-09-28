"""Image processing module — Pillow-based normalisation (spec §5.2 line 4634).

Accepts JPEG, PNG, GIF, WebP, and HEIC images. Validates via magic bytes,
auto-orients via EXIF, resizes the longest side to the protocol-defined
maximum, and converts to WebP at the protocol-defined quality.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid

import pillow_heif
from PIL import Image, ImageOps
from PIL.Image import Resampling

from ..domain.media_constraints import (
    IMAGE_ACCEPTED_MIMES,
    IMAGE_MAX_DIMENSION,
    IMAGE_WEBP_QUALITY,
    SPACE_IMAGE_FIT_MIN_DIMENSION,
    THUMBNAIL_PX,
    THUMBNAIL_WEBP_QUALITY,
)

# Modern Android camera (Samsung 2024+, Pixel HEIF-on) and iOS default
# to HEIC. Pillow alone can't decode HEIC bytes; pillow_heif registers a
# Pillow image opener so ``Image.open(heic_bytes)`` works the same way
# JPEG/PNG/WebP do. Without this the magic-byte check passes (we
# advertise ``image/heic`` in :data:`IMAGE_ACCEPTED_MIMES`) but
# ``Image.open`` raises ``cannot identify image file`` and the upload
# 422s with "Cannot open image" — the symptom Pascal hit on the HA
# Android Companion App.
pillow_heif.register_heif_opener()

log = logging.getLogger(__name__)

#: Hard cap on WebP encodes one :meth:`ImageProcessor.fit_within` spends
#: (full-size try + binary search + min-dimension fallback). Ten halvings of
#: a ~2000 px range land within a few pixels of the largest fitting size.
_FIT_MAX_ENCODES: int = 10

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
    defined in :mod:`socialhome.domain.media_constraints`.
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
        # Pillow's decode + resize + encode are CPU-bound and would
        # stall the event loop on every upload. Mirror the pattern in
        # ``video_processor`` / ``audio_processor`` and run the
        # synchronous body on the default thread pool.
        return await asyncio.to_thread(self._process_sync, data, filename)

    def _process_sync(self, data: bytes, filename: str) -> tuple[bytes, str]:
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
        return await asyncio.to_thread(self._generate_thumbnail_sync, data, size)

    def _generate_thumbnail_sync(self, data: bytes, size: int) -> bytes:
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

    async def is_valid_webp(self, data: bytes, *, max_dimension: int) -> bool:
        """``True`` when *data* is a WebP that fully decodes and whose
        longest side is at most *max_dimension*.

        For image bytes another household sent us (a space cover / icon in
        a snapshot or a config change): the dimension is read from the
        header BEFORE any pixel is decoded, so an oversized or
        decompression-bomb image costs a header parse, not a decode.
        """
        return await asyncio.to_thread(self._is_valid_webp_sync, data, max_dimension)

    def _is_valid_webp_sync(self, data: bytes, max_dimension: int) -> bool:
        try:
            img = Image.open(io.BytesIO(data))
            if img.format != "WEBP" or max(img.size) > max_dimension:
                return False
            img.load()
        except Exception:
            return False
        return True

    async def fit_within(
        self,
        data: bytes,
        max_bytes: int,
        *,
        min_dimension: int = SPACE_IMAGE_FIT_MIN_DIMENSION,
    ) -> bytes | None:
        """Return *data* when it is at most *max_bytes*, else a smaller WebP
        rendition of it that is — or ``None`` when even a *min_dimension*
        rendition does not fit.

        For images that must travel inside a size-capped envelope (a space
        cover / icon in an invite snapshot). The image is shrunk, never
        cropped, and never upscaled, at :data:`THUMBNAIL_WEBP_QUALITY`. A
        full-size re-encode is tried first; failing that, the longest side
        is binary-searched between *min_dimension* and the source's, keeping
        the largest rendition that fits — so the result lands just under the
        budget instead of well below it. At most :data:`_FIT_MAX_ENCODES`
        encodes are spent, bounding the worst case.

        Raises
        ------
        ValueError
            If *data* is over the bound and Pillow cannot open it.
        """
        if len(data) <= max_bytes:
            return data
        return await asyncio.to_thread(
            self._fit_within_sync,
            data,
            max_bytes,
            min_dimension,
        )

    def _fit_within_sync(
        self,
        data: bytes,
        max_bytes: int,
        min_dimension: int,
    ) -> bytes | None:
        img: Image.Image
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:
            raise ValueError(f"Cannot open image to fit it: {exc}") from exc
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        src_dim = max(img.size)
        encodes = 0

        def encode(dim: int) -> bytes:
            nonlocal encodes
            encodes += 1
            out = io.BytesIO()
            self._resize(img, dim).save(
                out,
                format="WEBP",
                quality=THUMBNAIL_WEBP_QUALITY,
            )
            return out.getvalue()

        full = encode(src_dim)
        if len(full) <= max_bytes:
            return full
        # Invariant: ``hi`` is known too big; ``best`` (if set) is the
        # rendition at ``lo``, the largest dimension known to fit. Leave one
        # encode spare for the min-dimension fallback below.
        lo, hi = min_dimension - 1, src_dim
        best: bytes | None = None
        while hi - lo > 1 and encodes < _FIT_MAX_ENCODES - 1:
            mid = (lo + hi) // 2
            out = encode(mid)
            if len(out) <= max_bytes:
                lo, best = mid, out
            else:
                hi = mid
        if best is None and hi > min_dimension:
            # The budget ran out before any probe fitted; the smallest
            # acceptable rendition is the last resort.
            out = encode(min_dimension)
            if len(out) <= max_bytes:
                best = out
        return best

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

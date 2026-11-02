"""Screensaver photos for wall panels, as panel-sized PNGs.

The gallery stores every photo as one WebP (≤ 2560 px,
``domain/media_constraints.py``). An ESPHome panel can't decode WebP, so
``GET /api/display/v1/photo/{id}.png?w=&h=`` draws the stored WebP
fitted into the panel's screen (aspect kept, never enlarged) as a PNG —
on the fly, never stored (owner decision 2026-11-02). Who may see which
photo is decided on every request by
:class:`~socialhome.services.dashboard_photo_frame_service.DashboardPhotoFrameService`;
this module only draws — with Pillow in a worker thread — and keeps the
last few pictures in memory (:data:`CACHE_ENTRIES`, at most
:data:`CACHE_BYTES`), so the panels of one household showing the same
round share one drawing.
"""

from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from typing import Final

from PIL import Image, ImageOps

__all__ = [
    "CACHE_BYTES",
    "CACHE_ENTRIES",
    "DEFAULT_SIZE",
    "MAX_SIZE",
    "MIN_SIZE",
    "PhotoPictures",
    "clamp_size",
    "fitted_size",
    "panel_png",
]

#: The panel's screen when it names none (``?w=&h=`` absent): the
#: 1280 × 800 panels the firmware drives.
DEFAULT_SIZE: Final = (1280, 800)
MIN_SIZE: Final = (200, 200)
MAX_SIZE: Final = (1920, 1200)
#: Pictures kept (a round is 30 photos; panels show the same one at once).
CACHE_ENTRIES: Final = 8
#: …and at most this many bytes of them (a 1280 × 800 photo PNG is ~1–2 MB).
CACHE_BYTES: Final = 24 * 1024 * 1024


def clamp_size(w: int | None, h: int | None) -> tuple[int, int]:
    """A panel's ``?w=&h=`` held to :data:`MIN_SIZE` … :data:`MAX_SIZE`
    (``None`` → :data:`DEFAULT_SIZE`)."""
    width = DEFAULT_SIZE[0] if w is None else min(max(w, MIN_SIZE[0]), MAX_SIZE[0])
    height = DEFAULT_SIZE[1] if h is None else min(max(h, MIN_SIZE[1]), MAX_SIZE[1])
    return width, height


def fitted_size(width: int, height: int, box_w: int, box_h: int) -> tuple[int, int]:
    """A ``width`` × ``height`` picture fitted into ``box_w`` × ``box_h``:
    aspect kept, never enlarged, at least 1 × 1."""
    if width <= 0 or height <= 0:
        return 1, 1
    scale = min(box_w / width, box_h / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))


def panel_png(body: bytes, box_w: int, box_h: int) -> bytes:
    """``body`` (the stored WebP) fitted into ``box_w`` × ``box_h``
    (:func:`fitted_size`) as an opaque RGB PNG. Sync and CPU-bound: call
    it through :func:`asyncio.to_thread`."""
    with Image.open(io.BytesIO(body)) as img:
        img.load()
        # Stored photos are upright already; honour a tag anyway.
        upright = ImageOps.exif_transpose(img)
        rgb = upright.convert("RGB")
    size = fitted_size(rgb.width, rgb.height, box_w, box_h)
    if size != rgb.size:
        rgb = rgb.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
    out = io.BytesIO()
    # Level 6 (zlib's default): a big photo PNG barely shrinks further,
    # and higher levels cost seconds of CPU.
    rgb.save(out, format="PNG", compress_level=6)
    return out.getvalue()


class PhotoPictures:
    """A bounded LRU of drawn panel photos, keyed by ``(item id, stored
    file, width, height)`` — a re-processed photo is a new file name."""

    __slots__ = ("_bytes", "_cache", "_max", "_max_bytes")

    def __init__(
        self, *, max_entries: int = CACHE_ENTRIES, max_bytes: int = CACHE_BYTES
    ) -> None:
        self._cache: OrderedDict[tuple[str, str, int, int], bytes] = OrderedDict()
        self._max = max_entries
        self._max_bytes = max_bytes
        self._bytes = 0

    def cached(self, item_id: str, filename: str, w: int, h: int) -> bytes | None:
        """The kept picture, if any (marked as just used)."""
        key = (item_id, filename, w, h)
        found = self._cache.get(key)
        if found is not None:
            self._cache.move_to_end(key)
        return found

    async def render(
        self, item_id: str, filename: str, body: bytes, w: int, h: int
    ) -> bytes:
        """Draw ``body`` for a ``w`` × ``h`` screen and keep it."""
        png = await asyncio.to_thread(panel_png, body, w, h)
        key = (item_id, filename, w, h)
        old = self._cache.pop(key, None)
        if old is not None:
            self._bytes -= len(old)
        self._cache[key] = png
        self._bytes += len(png)
        while self._cache and (
            len(self._cache) > self._max or self._bytes > self._max_bytes
        ):
            _, dropped = self._cache.popitem(last=False)
            self._bytes -= len(dropped)
        return png

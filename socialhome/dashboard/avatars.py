"""Profile pictures for wall panels, as square PNGs.

Profile pictures are stored as WebP, which an ESPHome ``online_image``
can't decode; ``GET /api/display/v1/avatar/{user_id}?size=N`` serves
them as an ``N`` × ``N`` PNG instead. Who may be shown is decided by the
read model on every request
(:meth:`~socialhome.services.household_dashboard_service.HouseholdDashboardService.avatar`,
the same rule as the tablet's ``/avatar/…``); this module only
transcodes — with Pillow in a worker thread — and keeps a small LRU of
results keyed by ``(picture tag, size)``.
"""

from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from typing import Final

from PIL import Image, ImageOps

__all__ = [
    "AVATAR_SIZES",
    "CACHE_ENTRIES",
    "DEFAULT_AVATAR_SIZE",
    "AvatarImages",
    "square_png",
]

#: Sizes a panel may ask for (``?size=N``).
AVATAR_SIZES: Final = frozenset({48, 64, 96, 128, 192})
DEFAULT_AVATAR_SIZE: Final = 96
#: Transcoded pictures kept (a household of a dozen people × a few sizes).
CACHE_ENTRIES: Final = 64


def square_png(body: bytes, size: int) -> bytes:
    """``body`` (any image Pillow reads) centre-cropped to a square and
    scaled to ``size`` × ``size``, as an RGBA PNG. Sync and CPU-bound:
    call it through :func:`asyncio.to_thread`."""
    with Image.open(io.BytesIO(body)) as img:
        img.load()
        rgba = img.convert("RGBA")
    square = ImageOps.fit(rgba, (size, size), method=Image.Resampling.LANCZOS)
    out = io.BytesIO()
    square.save(out, format="PNG", optimize=True)
    return out.getvalue()


class AvatarImages:
    """A bounded LRU of transcoded avatars."""

    __slots__ = ("_cache", "_max")

    def __init__(self, *, max_entries: int = CACHE_ENTRIES) -> None:
        self._cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._max = max_entries

    async def png(self, body: bytes, tag: str, size: int) -> bytes:
        """``body`` as a ``size`` PNG; ``tag`` identifies the picture (its
        hash), so a new picture is a new cache entry."""
        key = (tag, size)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        out = await asyncio.to_thread(square_png, body, size)
        self._cache[key] = out
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return out

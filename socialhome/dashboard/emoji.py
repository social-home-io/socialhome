"""Emoji as pictures for wall panels — the bundled Twemoji subset.

A wall panel (an ESPHome LVGL display) has no colour-emoji font, so the
panel API names every emoji it shows by its *Twemoji code* — the
lower-case hex code points joined by ``-``, without the emoji variation
selector ``U+FE0F`` unless the emoji is a zero-width-joiner sequence
(Twemoji's own file naming) — and serves it as a PNG from
``GET /api/display/v1/emoji/{code}?size=N``.

The PNGs are Twemoji 72 × 72 (github.com/jdecked/twemoji v17.0.3,
graphics CC-BY 4.0 — see ``static/emoji/LICENSE.md``), bundled under
:data:`EMOJI_DIR`: only the subset the dashboard can actually show
(``scripts/fetch_twemoji.py``). Anything else falls back to 📍
(:data:`FALLBACK_CODE`). Nothing is fetched at runtime.

:class:`EmojiImages` reads a file with ``aiofiles`` on first use,
scales it with Pillow in a worker thread and keeps a small LRU of the
results.
"""

from __future__ import annotations

import asyncio
import io
import re
import unicodedata
from collections import OrderedDict
from pathlib import Path
from typing import Final

import aiofiles
import aiofiles.os
from PIL import Image

__all__ = [
    "CODE_PATTERN",
    "EMOJI_DIR",
    "EMOJI_SIZES",
    "FALLBACK_CODE",
    "SOURCE_SIZE",
    "EmojiImages",
    "emoji_code",
    "is_code",
    "scale_png",
]

#: The bundled Twemoji PNGs (``<code>.png``).
EMOJI_DIR: Final = Path(__file__).parent / "static" / "emoji"
#: The bundled PNGs' edge length (pixels).
SOURCE_SIZE: Final = 72
#: Sizes a panel may ask for (``?size=N``).
EMOJI_SIZES: Final = frozenset({24, 32, 48, 64, 72, 96, 128})
#: 📍 — what an emoji the bundle lacks is drawn as.
FALLBACK_CODE: Final = "1f4cd"
#: A Twemoji code as the API accepts it (a path segment: no traversal).
CODE_PATTERN: Final = r"[0-9a-f]{2,6}(?:-[0-9a-f]{2,6}){0,9}"
_CODE = re.compile(CODE_PATTERN)
_ZWJ = 0x200D
_VS16 = 0xFE0F
#: Longest text :func:`emoji_code` treats as one emoji (code points).
_MAX_POINTS = 16
#: Scaled images kept (one panel shows a few dozen at once).
CACHE_ENTRIES: Final = 512


def emoji_code(text: str | None) -> str | None:
    """``text``'s Twemoji code ("🏊" → ``"1f3ca"``, "☀️" → ``"2600"``,
    "❤️‍🔥" → ``"2764-fe0f-200d-1f525"``) — ``None`` when ``text`` is
    no emoji at all: empty, longer than an emoji, or holding a letter,
    digit, space or punctuation (a zone pin may be a letter)."""
    if not text or len(text) > _MAX_POINTS:
        return None
    if any(unicodedata.category(ch)[0] in "LNZPC" and ord(ch) != _ZWJ for ch in text):
        return None
    points = [ord(ch) for ch in text]
    if _ZWJ not in points:
        points = [p for p in points if p != _VS16]
    if not points:
        return None
    return "-".join(f"{p:x}" for p in points)


def is_code(value: str) -> bool:
    """Is ``value`` shaped like a Twemoji code (what the route accepts)?"""
    return _CODE.fullmatch(value) is not None


def scale_png(body: bytes, size: int) -> bytes:
    """``body`` (an image) as a ``size`` × ``size`` RGBA PNG. Sync and
    CPU-bound: call it through :func:`asyncio.to_thread`."""
    with Image.open(io.BytesIO(body)) as img:
        img.load()
        rgba = img.convert("RGBA")
    if rgba.size != (size, size):
        rgba = rgba.resize((size, size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    rgba.save(out, format="PNG", optimize=True)
    return out.getvalue()


class EmojiImages:
    """The bundled emoji PNGs at the sizes panels ask for."""

    __slots__ = ("_cache", "_dir", "_max")

    def __init__(
        self, directory: Path = EMOJI_DIR, *, max_entries: int = CACHE_ENTRIES
    ) -> None:
        self._dir = directory
        self._cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._max = max_entries

    async def resolve(self, code: str) -> str:
        """``code`` if the bundle has it (also tried without ``fe0f``),
        else :data:`FALLBACK_CODE`. ``code`` must pass :func:`is_code`."""
        candidates = [code]
        bare = "-".join(p for p in code.split("-") if p != "fe0f")
        if bare and bare != code:
            candidates.append(bare)
        for candidate in candidates:
            if await aiofiles.os.path.isfile(self._dir / f"{candidate}.png"):
                return candidate
        return FALLBACK_CODE

    async def png(self, code: str, size: int) -> bytes:
        """The PNG of a code :meth:`resolve` returned, ``size`` pixels."""
        key = (code, size)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        async with aiofiles.open(self._dir / f"{code}.png", "rb") as fh:
            body = await fh.read()
        if size != SOURCE_SIZE:
            body = await asyncio.to_thread(scale_png, body, size)
        self._cache[key] = body
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return body

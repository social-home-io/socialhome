"""Colour validation and WCAG contrast for the household dashboard.

Every colour the dashboard puts into a page comes from somewhere a user
or admin typed it (calendar, sticky note, household theme). It reaches
the page only through the hash-admitted inline ``<style>`` — and the
hash admits whatever text it covers — so :func:`hex_color` lets through
nothing but a plain ``#rgb`` / ``#rrggbb`` (alpha shapes are refused:
their contrast depends on what's behind them).

Contrast follows WCAG 2.x: relative luminance from sRGB, ratio
``(L1 + 0.05) / (L2 + 0.05)``. :data:`AA_TEXT` (4.5:1) is the bar for
text; :func:`ink_on` picks black or white text for a coloured fill —
one of the two always clears it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = [
    "AA_TEXT",
    "BLACK",
    "WHITE",
    "contrast_ratio",
    "hex_color",
    "ink_on",
    "readable_on",
    "relative_luminance",
]

#: WCAG AA minimum contrast for normal-size text.
AA_TEXT = 4.5
BLACK = "#000000"
WHITE = "#ffffff"

_HEX = re.compile(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})")


def hex_color(value: object) -> str | None:
    """``value`` as a lower-case ``#rrggbb``, or ``None`` when it is
    anything but a plain 3- or 6-digit hex colour."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if _HEX.fullmatch(text) is None:
        return None
    if len(text) == 4:
        text = "#" + "".join(c * 2 for c in text[1:])
    return text


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` colour (0 … 1)."""
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio of two ``#rrggbb`` colours (1 … 21)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ink_on(fill: str) -> str:
    """Black or white — whichever reads better on ``fill``."""
    return (
        BLACK if contrast_ratio(BLACK, fill) >= contrast_ratio(WHITE, fill) else WHITE
    )


def readable_on(color: str, surfaces: Iterable[str], minimum: float = AA_TEXT) -> bool:
    """Does ``color`` reach ``minimum`` contrast as text on every one of
    ``surfaces``?"""
    return all(contrast_ratio(color, s) >= minimum for s in surfaces)

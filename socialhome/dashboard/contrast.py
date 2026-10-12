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
one of the two always clears it; :func:`note_ink` softens black to the
board's warm text colour for note cards where that still reads.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "AA_TEXT",
    "BLACK",
    "NOTE_DARK_BG",
    "NOTE_LIGHT_BG",
    "NOTE_MIX_DARK",
    "NOTE_MIX_LIGHT",
    "WARM_INK",
    "WHITE",
    "NoteColors",
    "contrast_ratio",
    "hex_color",
    "ink_on",
    "mix_oklab",
    "note_colors",
    "note_ink",
    "readable_on",
    "relative_luminance",
]

#: WCAG AA minimum contrast for normal-size text.
AA_TEXT = 4.5
BLACK = "#000000"
WHITE = "#ffffff"
#: The board's text colour (``--dash-text``): a softer black for notes.
WARM_INK = "#1f1d1a"

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
    return _to_linear(value / 255)


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


def note_ink(fill: str) -> str:
    """The ink for a note card on ``fill``: the board's own warm text
    colour (:data:`WARM_INK`) where black would win and the warm ink
    still reaches :data:`AA_TEXT`; otherwise :func:`ink_on`'s pick."""
    ink = ink_on(fill)
    if ink == BLACK and contrast_ratio(WARM_INK, fill) >= AA_TEXT:
        return WARM_INK
    return ink


#: Note cards are the household colour softened toward the page
#: (``--dash-bg``): this much of the colour in light mode, this much in
#: dark mode (and night) — mixed in Oklab, here on the server, so the ink
#: is picked against the fill the tablet actually draws.
NOTE_LIGHT_BG = "#f4f2ee"
NOTE_DARK_BG = "#151412"
NOTE_MIX_LIGHT = 0.82
NOTE_MIX_DARK = 0.75


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _oklab(color: str) -> tuple[float, float, float]:
    r, g, b = (_to_linear(int(color[i : i + 2], 16) / 255) for i in (1, 3, 5))
    lms = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    l_, m_, s_ = (v ** (1 / 3) for v in lms)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _from_oklab(lab: tuple[float, float, float]) -> str:
    big_l, a, b = lab
    l_ = big_l + 0.3963377774 * a + 0.2158037573 * b
    m_ = big_l - 0.1055613458 * a - 0.0638541728 * b
    s_ = big_l - 0.0894841775 * a - 1.2914855480 * b
    lms = (l_**3, m_**3, s_**3)
    rgb = (
        4.0767416621 * lms[0] - 3.3077115913 * lms[1] + 0.2309699292 * lms[2],
        -1.2684380046 * lms[0] + 2.6097574011 * lms[1] - 0.3413193965 * lms[2],
        -0.0041960863 * lms[0] - 0.7034186147 * lms[1] + 1.7076147010 * lms[2],
    )
    return "#" + "".join(f"{round(_to_srgb(c) * 255):02x}" for c in rgb)


def mix_oklab(color: str, ground: str, weight: float) -> str:
    """``color`` mixed with ``ground`` in Oklab — ``weight`` of ``color``
    (CSS ``color-mix(in oklab, color weight, ground)``), clipped to sRGB."""
    if color == ground:
        return color
    a, b = _oklab(color), _oklab(ground)
    return _from_oklab(
        (
            a[0] * weight + b[0] * (1 - weight),
            a[1] * weight + b[1] * (1 - weight),
            a[2] * weight + b[2] * (1 - weight),
        )
    )


@dataclass(slots=True, frozen=True)
class NoteColors:
    """A note card's softened fill and its ink, light and dark."""

    light_bg: str
    light_ink: str
    dark_bg: str
    dark_ink: str


def note_colors(color: str) -> NoteColors:
    """The note card for household colour ``color`` (``#rrggbb``): its
    fill softened toward each page background, and :func:`note_ink`
    picked against that fill — so the ink reaches :data:`AA_TEXT` on what
    is drawn, not on the raw colour."""
    light = mix_oklab(color, NOTE_LIGHT_BG, NOTE_MIX_LIGHT)
    dark = mix_oklab(color, NOTE_DARK_BG, NOTE_MIX_DARK)
    return NoteColors(light, note_ink(light), dark, note_ink(dark))


def readable_on(color: str, surfaces: Iterable[str], minimum: float = AA_TEXT) -> bool:
    """Does ``color`` reach ``minimum`` contrast as text on every one of
    ``surfaces``?"""
    return all(contrast_ratio(color, s) >= minimum for s in surfaces)

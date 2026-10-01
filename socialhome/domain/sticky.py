"""Sticky-note domain type + field rules (§19).

The field rules are shared by every write path — the REST service
(strict: a bad value is refused) and federation inbound / snapshot sync
(lenient: a bad value from a peer is replaced by a safe one, never
stored raw):

* ``color`` is rendered by the SPA as a CSS ``background`` value, so
  anything but a hex colour (``url(...)``, ``red; ...``) would turn a note
  into a cross-household tracking beacon or a style injection. Only
  ``#RGB`` / ``#RRGGBB`` is accepted, canonicalised to upper-case
  ``#RRGGBB``.
* ``position_x`` / ``position_y`` live in the SPA board's normalised
  ``STICKY_BOARD_WIDTH`` x ``STICKY_BOARD_HEIGHT`` coordinate space
  (``client/src/features/stickies/StickyBoardPage.tsx``) and are clamped
  into it.
* ``content`` is user text: control and bidi-spoofing characters are
  stripped (:func:`socialhome.domain.task.sanitize_block`, newlines kept)
  and it is capped at :data:`MAX_STICKY_CONTENT_LENGTH`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

from .task import sanitize_block

#: Default note colour (the SPA's first palette swatch).
DEFAULT_STICKY_COLOR: Final = "#FFF9B1"

#: The SPA board's normalised coordinate space (``BOARD_W`` / ``BOARD_H``).
STICKY_BOARD_WIDTH: Final = 1000.0
STICKY_BOARD_HEIGHT: Final = 700.0

#: Content cap. The SPA editor allows 500; the extra headroom keeps a
#: slightly different client from being refused while still bounding
#: what a peer can make every member household store.
MAX_STICKY_CONTENT_LENGTH: Final = 2000

_HEX_COLOR: Final = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


@dataclass(slots=True, frozen=True)
class Sticky:
    """One sticky note. ``space_id=None`` means household-scope."""

    id: str
    author: str  # user_id
    content: str
    color: str
    position_x: float
    position_y: float
    created_at: str
    updated_at: str
    space_id: str | None = None  # None = household board


def normalize_sticky_color(raw: object) -> str | None:
    """``#RGB`` / ``#RRGGBB`` (any case) as upper-case ``#RRGGBB``;
    ``None`` for anything else (named colours, ``url(...)``, non-strings)."""
    if not isinstance(raw, str):
        return None
    m = _HEX_COLOR.fullmatch(raw)
    if m is None:
        return None
    digits = m.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.upper()


def parse_sticky_coord(raw: object, *, limit: float) -> float | None:
    """A finite number clamped into ``[0, limit]``; ``None`` when ``raw``
    is not a number (bools included), not finite, or too large to be a
    float (a 400-digit JSON integer raises :class:`OverflowError`)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except OverflowError:
        return None
    if not math.isfinite(value):
        return None
    return min(max(value, 0.0), limit)


def sanitize_sticky_content(raw: str) -> str:
    """Control / bidi-spoofing characters stripped, trimmed (newlines and
    tabs kept); ``""`` when nothing visible is left. Not length-capped —
    the caller decides between refusing and truncating."""
    return sanitize_block(raw)


@dataclass(slots=True, frozen=True)
class PeerStickyFields:
    """A peer-supplied sticky's display fields after the lenient rules.

    ``content`` is ``""`` when nothing visible was left (the caller drops
    the event); ``truncated`` tells the caller to log the cut.
    """

    content: str
    color: str
    position_x: float
    position_y: float
    truncated: bool


def coerce_peer_sticky(
    *,
    content: object,
    color: object,
    position_x: object,
    position_y: object,
) -> PeerStickyFields:
    """Apply the sticky field rules to a federated / synced payload.

    Lenient where the REST service is strict — a peer's bad value is
    replaced by a safe one instead of failing the whole event, but the
    raw value is never stored: a non-hex colour becomes
    :data:`DEFAULT_STICKY_COLOR`, a non-numeric / non-finite coordinate
    ``0.0`` (finite ones are clamped into the board), and content is
    sanitised and cut to :data:`MAX_STICKY_CONTENT_LENGTH`.
    """
    text = sanitize_sticky_content(content) if isinstance(content, str) else ""
    truncated = len(text) > MAX_STICKY_CONTENT_LENGTH
    if truncated:
        text = sanitize_sticky_content(text[:MAX_STICKY_CONTENT_LENGTH])
    return PeerStickyFields(
        content=text,
        color=normalize_sticky_color(color) or DEFAULT_STICKY_COLOR,
        position_x=parse_sticky_coord(position_x, limit=STICKY_BOARD_WIDTH) or 0.0,
        position_y=parse_sticky_coord(position_y, limit=STICKY_BOARD_HEIGHT) or 0.0,
        truncated=truncated,
    )

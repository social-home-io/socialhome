"""Single-emoji predicate shared by every "pick an icon" field.

Timetable lessons and space zones both carry an optional icon that must
be exactly one emoji. :func:`is_single_emoji` is the one pure check;
callers wrap it in their own domain exception.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

#: Code points one icon may span — room for ZWJ sequences and tag flags.
MAX_EMOJI_CODEPOINTS: Final = 16

_ZWJ: Final = 0x200D
#: The only combining marks an emoji may carry: text / emoji variation
#: selectors and the keycap mark. Any other ``M*`` would let a sender
#: stack marks ("Zalgo") that bleed over the marker.
_EMOJI_MARKS: Final = frozenset({0xFE0E, 0xFE0F, 0x20E3})
_REGIONAL_INDICATORS: Final = range(0x1F1E6, 0x1F200)
_SKIN_TONES: Final = range(0x1F3FB, 0x1F400)
#: Keycaps (1️⃣ #️⃣ *⃣) are the one emoji built on an ASCII base.
_KEYCAP_RE: Final = re.compile("[0-9#*]\ufe0f?\u20e3")
#: Subdivision flags (🏴 England / Scotland / Wales) are the one emoji
#: built from tag characters: a black flag, a 2–6 character lower-case
#: subdivision code, then CANCEL TAG. Tags anywhere else are refused —
#: they are invisible and would smuggle ASCII text through the field.
_TAG_FLAG_RE: Final = re.compile(
    "\U0001f3f4[\U000e0030-\U000e0039\U000e0061-\U000e007a]{2,6}\U000e007f"
)


def _pictographs(value: str) -> int:
    """Count visible pictographs, grapheme-ish.

    A symbol starts a new one unless it follows a ZWJ (👩‍🔬 is one);
    two regional indicators make one flag (🇬🇧); skin tones, variation
    selectors, keycap marks and tag characters never start one.
    """
    count = 0
    prev = 0
    open_flag = False
    for c in value:
        o = ord(c)
        if o in _REGIONAL_INDICATORS:
            if open_flag:
                open_flag = False  # second half of the flag
            else:
                count += prev != _ZWJ
                open_flag = True
        else:
            open_flag = False
            if unicodedata.category(c)[0] == "S" and o not in _SKIN_TONES:
                count += prev != _ZWJ
        prev = o
    return count


def is_single_emoji(value: object) -> bool:
    """True when *value* is exactly one emoji of ≤ :data:`MAX_EMOJI_CODEPOINTS`.

    A keycap (1️⃣ #️⃣ *⃣) is matched explicitly. Anything else must start
    with a symbol (``S*``, regional indicators included), consist only of
    non-ASCII symbols, variation selectors / the keycap mark and ZWJs,
    and draw a single pictograph. A subdivision flag is matched
    explicitly; tag characters are refused anywhere else.
    That admits 🔢 ✏️ 👩‍🔬 👍🏽 🇬🇧 but refuses words in any script, markup,
    whitespace, invisible controls, bare joiners, stacked marks, hidden
    tag text and ★★.
    """
    if not isinstance(value, str):
        return False
    if _KEYCAP_RE.fullmatch(value) or _TAG_FLAG_RE.fullmatch(value):
        return True
    return (
        1 <= len(value) <= MAX_EMOJI_CODEPOINTS
        and unicodedata.category(value[0])[0] == "S"
        and all(
            ord(c) > 0x7F
            and (
                ord(c) == _ZWJ
                or ord(c) in _EMOJI_MARKS
                or unicodedata.category(c)[0] == "S"
            )
            for c in value
        )
        and _pictographs(value) == 1
    )

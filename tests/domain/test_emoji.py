"""Tests for :mod:`socialhome.domain.emoji` — the single-emoji predicate."""

from __future__ import annotations

import pytest

from socialhome.domain.emoji import MAX_EMOJI_CODEPOINTS, is_single_emoji


@pytest.mark.parametrize(
    "value",
    ["🔢", "✏️", "👩‍🔬", "👍🏽", "🇬🇧", "⚽", "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "1️⃣", "#️⃣", "*⃣", "★", "🏠"],
)
def test_single_emoji_accepted(value):
    assert is_single_emoji(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "<b>",
        "🔢" * 17,
        " 🔢",
        "🔢 ",
        " 🔢",  # non-ASCII whitespace
        "",
        "日本",  # a word in another script
        "\u202e🔢",  # bidi override
        "a⃣",  # a keycap on a letter is not a keycap
        "‍",  # a bare ZWJ
        "́️",  # marks only
        "\U0001f3fd",  # a skin tone on its own
        "★★",
        "★" * 16,
        "🔢🔢",
        "🇬🇧🇬🇧",  # two flags
        5,
        None,
        b"\xf0\x9f\x94\xa2",
    ],
)
def test_not_a_single_emoji_rejected(value):
    assert is_single_emoji(value) is False


def test_code_point_cap():
    assert MAX_EMOJI_CODEPOINTS == 16
    # A ZWJ family sequence at exactly the cap is one pictograph.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
    assert len(family) <= MAX_EMOJI_CODEPOINTS
    assert is_single_emoji(family) is True
    assert is_single_emoji("🔢" + "\ufe0f" * MAX_EMOJI_CODEPOINTS) is False


def _tags(text: str) -> str:
    return "".join(chr(0xE0000 + ord(c)) for c in text)


def test_subdivision_flags_accepted():
    for code in ("gbeng", "gbsct", "gbwls", "usca"):
        assert is_single_emoji("🏴" + _tags(code) + "\U000e007f") is True


@pytest.mark.parametrize(
    "value",
    [
        # ASCII smuggled in tag characters ("ignore all prev").
        "🏴" + _tags("ignoreallprev"),
        "🏴" + _tags("ignore all") + "\U000e007f",
        # Tags without the cancel tag, or after a non-flag base.
        "🏴" + _tags("gbeng"),
        "🏫" + _tags("gbeng") + "\U000e007f",
        # Upper case / punctuation are not subdivision-code tags.
        "🏴" + _tags("GBENG") + "\U000e007f",
        # Stacked combining marks ("Zalgo") bleed over the marker.
        "🏫" + "\u0336" * 15,
        "🏫\u0301",
    ],
)
def test_smuggling_and_zalgo_rejected(value):
    assert is_single_emoji(value) is False

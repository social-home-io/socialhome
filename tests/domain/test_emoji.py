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
        "‮🔢",  # bidi override
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
    # One pictograph (a tag-sequence flag) padded with tag characters.
    at_cap = "🏴" + "\U000e0067" * 15
    assert is_single_emoji(at_cap) is True
    assert is_single_emoji(at_cap + "\U000e0067") is False

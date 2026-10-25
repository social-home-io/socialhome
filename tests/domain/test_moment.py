"""Tests for socialhome.domain.moment — the hashtag rule.

The SPA's ``client/src/features/momentum/hashtags.tsx`` applies the same
rule; ``hashtags.test.tsx`` mirrors these cases so a chip the user sees
is the tag the server stored.
"""

from __future__ import annotations

import unicodedata

from socialhome.domain.moment import (
    MOMENT_MAX_HASHTAG_LEN,
    MOMENT_MAX_HASHTAGS_PER_POST,
    extract_hashtags,
    normalize_hashtag,
)


def test_ascii_tags_lowercase_in_first_seen_order():
    assert extract_hashtags("Trip #Berlin then #rome, #BERLIN again") == [
        "berlin",
        "rome",
    ]


def test_letters_beyond_ascii_are_part_of_the_tag():
    """``#Kürbisfest`` was stored as ``k`` and ``#Fußball`` as ``fu``."""
    assert extract_hashtags("Bestes Wochenende!! #Kürbisfest #Fußball #herbst2026") == [
        "kürbisfest",
        "fußball",
        "herbst2026",
    ]
    assert extract_hashtags("#Ελλάδα #Москва #東京 #café_au_lait") == [
        "ελλάδα",
        "москва",
        "東京",
        "café_au_lait",
    ]


def test_combining_marks_stay_in_the_tag_and_compose():
    """A decomposed ``u + ¨`` is the same tag as the composed ``ü``, and
    scripts that need marks (Devanagari) keep the whole word."""
    decomposed = unicodedata.normalize("NFD", "#Kürbisfest")
    assert extract_hashtags(decomposed) == ["kürbisfest"]
    assert extract_hashtags("#हिन्दी") == ["हिन्दी"]


def test_punctuation_and_emoji_end_a_tag():
    assert extract_hashtags("#fest! #fest2🎃 #a-b #x.y") == ["fest", "fest2", "a", "x"]


def test_mid_word_hash_is_not_a_tag_but_a_double_hash_is():
    assert extract_hashtags("issue#42 straße#nein ##ok") == ["ok"]


def test_a_tag_longer_than_the_cap_is_dropped_not_cut():
    ok = "ü" * MOMENT_MAX_HASHTAG_LEN
    assert extract_hashtags(f"#{ok} #{ok}x") == [ok]


def test_at_most_the_per_post_cap():
    text = " ".join(f"#t{i}" for i in range(MOMENT_MAX_HASHTAGS_PER_POST + 5))
    assert len(extract_hashtags(text)) == MOMENT_MAX_HASHTAGS_PER_POST


def test_normalize_hashtag_matches_the_extractor():
    assert normalize_hashtag(" #Kürbisfest ") == "kürbisfest"
    assert normalize_hashtag(unicodedata.normalize("NFD", "Kürbisfest")) == (
        "kürbisfest"
    )
    assert normalize_hashtag("") == ""

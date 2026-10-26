"""Tests for socialhome.domain.moment — the hashtag rule and lifetime.

The SPA's ``client/src/features/momentum/hashtags.tsx`` applies the same
rule; ``hashtags.test.tsx`` mirrors these cases so a chip the user sees
is the tag the server stored.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.moment import (
    MOMENT_DEFAULT_RETENTION_DAYS,
    MOMENT_MAX_HASHTAG_LEN,
    MOMENT_MAX_HASHTAGS_PER_POST,
    MOMENT_MAX_LIFETIME_DAYS,
    MOMENT_MIN_RETENTION_DAYS,
    MOMENT_REACTION_MAX_EMOJI_BYTES,
    MOMENT_RELAY_MAX_AGE_HOURS,
    REACTION_REACH_AUTHOR,
    REACTION_REACH_HOUSEHOLD,
    MomentReactionInvalidError,
    MomentTombstone,
    clamp_moment_expiry,
    extract_hashtags,
    is_valid_moment_reaction,
    moment_too_old_to_relay,
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


# ── Lifetime (v_65) ─────────────────────────────────────────────────────


_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_an_expiry_in_range_is_kept_byte_for_byte():
    expires = (_T0 + timedelta(days=12)).isoformat()
    assert clamp_moment_expiry(_T0.isoformat(), expires) == expires


def test_an_expiry_past_thirty_days_is_clamped():
    clamped = clamp_moment_expiry(
        _T0.isoformat(), (_T0 + timedelta(days=400)).isoformat()
    )
    assert datetime.fromisoformat(clamped) == _T0 + timedelta(
        days=MOMENT_MAX_LIFETIME_DAYS
    )


def test_naive_times_are_utc():
    clamped = clamp_moment_expiry("2026-10-01T12:00:00", "2027-10-01T12:00:00")
    assert datetime.fromisoformat(clamped) == _T0 + timedelta(days=30)


def test_exactly_thirty_days_is_kept():
    expires = (_T0 + timedelta(days=30)).isoformat()
    assert clamp_moment_expiry(_T0.isoformat(), expires) == expires


def test_an_unparseable_time_is_left_to_the_callers_checks():
    assert clamp_moment_expiry("junk", "2099-01-01T00:00:00") == "2099-01-01T00:00:00"
    assert clamp_moment_expiry(_T0.isoformat(), "junk") == "junk"


def test_relay_age_cutoff_is_48_hours():
    now = _T0
    assert not moment_too_old_to_relay((now - timedelta(hours=47)).isoformat(), now=now)
    assert moment_too_old_to_relay((now - timedelta(hours=49)).isoformat(), now=now)
    assert not moment_too_old_to_relay(now.isoformat(), now=now)
    assert moment_too_old_to_relay(None, now=now)
    assert moment_too_old_to_relay("", now=now)
    assert moment_too_old_to_relay("junk", now=now)
    assert MOMENT_RELAY_MAX_AGE_HOURS == 48


def test_only_a_tombstone_that_changed_something_relays():
    assert MomentTombstone.REMOVED.changed
    assert MomentTombstone.RECORDED.changed
    assert not MomentTombstone.ALREADY.changed


def test_retention_bounds():
    assert MOMENT_MIN_RETENTION_DAYS == 1
    assert MOMENT_DEFAULT_RETENTION_DAYS == 7
    assert MOMENT_MAX_LIFETIME_DAYS == 30


# ── v_67: a reaction is one emoji of ≤ 32 bytes ───────────────────────────


@pytest.mark.parametrize("emoji", ["❤️", "🔥", "👏", "👩‍👩‍👧‍👦", "🇬🇧", "1️⃣", "👍🏽"])
def test_a_reaction_is_one_emoji(emoji):
    assert len(emoji.encode()) <= MOMENT_REACTION_MAX_EMOJI_BYTES
    assert is_valid_moment_reaction(emoji)


@pytest.mark.parametrize(
    "emoji", ["", "a", "hello", "❤️❤️", "🔥 ", None, 5, "🔥" * 9, "<b>"]
)
def test_anything_else_is_no_reaction(emoji):
    assert not is_valid_moment_reaction(emoji)


def test_reaction_error_and_reach_values():
    assert MomentReactionInvalidError.status == 422
    assert MomentReactionInvalidError.code == "INVALID_REACTION"
    assert (REACTION_REACH_AUTHOR, REACTION_REACH_HOUSEHOLD) == ("author", "household")

"""Tests for the small user_preferences helper."""

from __future__ import annotations

import orjson
import pytest

from socialhome.domain.highlight import HighlightAudience
from socialhome.services.user_preferences import (
    DEFAULT_HIGHLIGHTS_MAX_COUNT,
    DEFAULT_HIGHLIGHTS_RETENTION_DAYS,
    MAX_TIMETABLE_HOME_PINS,
    parse_highlights_preferences,
    parse_timetable_home_pins,
)


def test_defaults_when_blob_is_empty():
    p = parse_highlights_preferences(None)
    assert p.retention_days == DEFAULT_HIGHLIGHTS_RETENTION_DAYS
    assert p.max_count == DEFAULT_HIGHLIGHTS_MAX_COUNT
    assert p.default_audience_kind is HighlightAudience.ALL_PAIRED
    assert p.default_audience == ()


def test_invalid_json_falls_back_to_defaults():
    p = parse_highlights_preferences("{not json")
    assert p.retention_days == DEFAULT_HIGHLIGHTS_RETENTION_DAYS


def test_clamps_out_of_range_values():
    """Values outside [_RETENTION_MIN, _MAX] clamp; nonsensical values use default."""
    p = parse_highlights_preferences(
        '{"highlights": {"retention_days": 99999, "max_count": 2}}'
    )
    assert p.retention_days == 90  # clamped to _RETENTION_MAX
    assert p.max_count == 10  # clamped to _MAX_COUNT_MIN

    p = parse_highlights_preferences('{"highlights": {"retention_days": "nope"}}')
    assert p.retention_days == DEFAULT_HIGHLIGHTS_RETENTION_DAYS


def test_users_audience_with_ids():
    p = parse_highlights_preferences(
        '{"highlights": {"default_audience": {"kind": "users", "ids": ["u1", "u2"]}}}'
    )
    assert p.default_audience_kind is HighlightAudience.USERS
    assert p.default_audience == ("u1", "u2")


def test_unknown_audience_kind_falls_back():
    p = parse_highlights_preferences(
        '{"highlights": {"default_audience": {"kind": "everyone"}}}'
    )
    assert p.default_audience_kind is HighlightAudience.ALL_PAIRED
    assert p.default_audience == ()


def test_blob_top_level_not_dict():
    """Defensive: when preferences_json itself is a list / scalar."""
    p = parse_highlights_preferences('"oops"')
    assert p.retention_days == DEFAULT_HIGHLIGHTS_RETENTION_DAYS


def test_highlights_block_not_dict():
    p = parse_highlights_preferences('{"highlights": "broken"}')
    assert p.retention_days == DEFAULT_HIGHLIGHTS_RETENTION_DAYS


# ─── Timetable home pins ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        (None, ()),
        ("", ()),
        ("not json", ()),
        ("[]", ()),
        ('{"timetable_home_pins": "tt-1"}', ()),
        ('{"timetable_home_pins": ["tt-1", "tt-2"]}', ("tt-1", "tt-2")),
        # Junk entries drop out; duplicates collapse, first one wins.
        (
            '{"timetable_home_pins": ["tt-2", 7, null, "", "tt-2", "tt-1"]}',
            ("tt-2", "tt-1"),
        ),
        ('{"timetable_home_pins": ["' + "x" * 65 + '", "ok"]}', ("ok",)),
    ],
)
def test_parse_timetable_home_pins(blob, expected):
    assert parse_timetable_home_pins(blob) == expected


def test_timetable_home_pins_are_capped():
    blob = orjson.dumps(
        {"timetable_home_pins": [f"tt-{i}" for i in range(500)]}
    ).decode()
    pins = parse_timetable_home_pins(blob)
    assert len(pins) == MAX_TIMETABLE_HOME_PINS
    assert pins[0] == "tt-0"

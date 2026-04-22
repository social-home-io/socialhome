"""Tests for socialhome.utils.datetime."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.utils.datetime import (
    parse_iso8601_lenient,
    parse_iso8601_optional,
    parse_iso8601_strict,
)


def test_strict_accepts_z_suffix():
    """Trailing ``Z`` is interpreted as UTC, matching fromisoformat's +00:00."""
    t = parse_iso8601_strict("2026-04-18T12:00:00Z")
    assert t == datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)


def test_strict_accepts_offset_suffix():
    """ISO with explicit offset round-trips correctly."""
    t = parse_iso8601_strict("2026-04-18T12:00:00+00:00")
    assert t.tzinfo is not None


def test_strict_raises_on_garbage():
    """Malformed input raises ValueError — callers that want lenient
    behaviour pick :func:`parse_iso8601_lenient` instead."""
    with pytest.raises(ValueError):
        parse_iso8601_strict("not-a-timestamp")


def test_optional_returns_none_for_empty_string():
    """Empty string is treated as missing and returns None."""
    assert parse_iso8601_optional("") is None


def test_optional_returns_none_for_none():
    """Explicit None input returns None without raising."""
    assert parse_iso8601_optional(None) is None


def test_optional_returns_none_for_garbage():
    """Malformed input returns None rather than raising."""
    assert parse_iso8601_optional("invalid") is None


def test_optional_round_trips_valid_timestamp():
    """Valid timestamps parse as they would via parse_iso8601_strict."""
    t = parse_iso8601_optional("2026-04-18T12:00:00Z")
    assert t is not None
    assert t.year == 2026 and t.month == 4 and t.day == 18


def test_lenient_falls_back_to_now_on_garbage():
    """parse_iso8601_lenient returns UTC now when input can't be parsed."""
    before = datetime.now(timezone.utc)
    t = parse_iso8601_lenient("garbage")
    after = datetime.now(timezone.utc)
    assert before <= t <= after
    assert t.tzinfo is not None


def test_lenient_falls_back_on_non_string():
    """Non-string input (e.g. integer from a malformed JSON) returns now."""
    before = datetime.now(timezone.utc)
    t = parse_iso8601_lenient(42)
    after = datetime.now(timezone.utc)
    assert before <= t <= after


def test_lenient_falls_back_on_none():
    """None input returns now rather than raising."""
    t = parse_iso8601_lenient(None)
    assert t.tzinfo is not None


def test_lenient_parses_valid_timestamps():
    """A valid ISO timestamp is parsed precisely, no fallback."""
    t = parse_iso8601_lenient("2026-04-18T12:00:00Z")
    assert t == datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)

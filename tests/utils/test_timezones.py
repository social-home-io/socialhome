"""Shared IANA timezone validation (:mod:`socialhome.utils.timezones`)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

import pytest

from socialhome.utils.timezones import (
    DEFAULT_TZ,
    coerce_tz,
    is_valid_tz,
    local_date,
    local_day_bounds,
    local_instant,
    parse_event_bound,
    zone_of,
)


@pytest.mark.parametrize(
    "name",
    ["UTC", "Europe/Zurich", "America/Los_Angeles"],
)
def test_is_valid_tz_accepts_real_zones(name):
    assert is_valid_tz(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Foo/Bar",  # well-formed key, not in the database
        "",  # empty
        "/etc/passwd",  # absolute path → ValueError, not KeyError
        "../../etc/passwd",  # traversal → ValueError
    ],
)
def test_is_valid_tz_rejects_unknown_and_malformed(name):
    assert is_valid_tz(name) is False


def test_coerce_tz_passes_through_a_valid_zone():
    assert coerce_tz("Europe/Zurich", context="test") == "Europe/Zurich"


def test_coerce_tz_falls_back_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="socialhome.utils.timezones"):
        assert coerce_tz("Foo/Bar", context="TEST_EVENT from instance i-x") == "UTC"
    # The offending value AND the sender are in the record so a
    # misbehaving peer is diagnosable rather than silent.
    assert "Foo/Bar" in caplog.text
    assert "i-x" in caplog.text


@pytest.mark.parametrize("missing", [None, ""])
def test_coerce_tz_treats_absent_as_default(missing, caplog):
    """An older peer omits the field entirely — that's not an error, so
    it must not log a warning."""
    with caplog.at_level(logging.WARNING, logger="socialhome.utils.timezones"):
        assert coerce_tz(missing, context="test") == DEFAULT_TZ
    assert caplog.text == ""


def test_is_valid_tz_rejects_overlong_name_instead_of_raising():
    """A 5000-char key makes the tz database lookup raise ``OSError``
    (ENAMETOOLONG) — that is "not a zone", not a crash."""
    assert is_valid_tz("a" * 5000) is False


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def test_zone_of_and_local_date():
    assert zone_of("Mars/Base") is UTC
    late = datetime(2026, 9, 27, 23, 30, tzinfo=UTC)
    assert local_date(late, "Europe/Berlin") == date(2026, 9, 28)
    assert local_date(late, "UTC") == date(2026, 9, 27)


class TestLocalInstant:
    def test_berlin_summer_is_utc_plus_two(self):
        got = local_instant(date(2026, 9, 28), hm("08:00"), "Europe/Berlin")
        assert got == datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
        assert got.tzinfo is UTC

    def test_unknown_zone_falls_back_to_utc(self):
        got = local_instant(date(2026, 9, 28), hm("08:00"), "Mars/Base")
        assert got == datetime(2026, 9, 28, 8, 0, tzinfo=UTC)

    def test_nonexistent_time_shifts_forward(self):
        # 2026-03-29 02:30 doesn't exist in Berlin (02:00 → 03:00): it
        # lands on 03:30 CEST, i.e. 01:30 UTC.
        got = local_instant(date(2026, 3, 29), hm("02:30"), "Europe/Berlin")
        assert got == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)

    def test_day_bounds_are_local_midnights(self):
        lo, hi = local_day_bounds(date(2026, 9, 28), "Europe/Berlin")
        assert lo == datetime(2026, 9, 27, 22, 0, tzinfo=UTC)
        assert hi == datetime(2026, 9, 28, 22, 0, tzinfo=UTC)

    def test_day_bounds_follow_dst(self):
        # 2026-10-25 is a 25-hour day in Berlin (CEST → CET).
        lo, hi = local_day_bounds(date(2026, 10, 25), "Europe/Berlin")
        assert hi - lo == timedelta(hours=25)

    def test_day_bounds_unknown_zone_is_utc(self):
        lo, hi = local_day_bounds(date(2026, 9, 28), "Mars/Base")
        assert lo == datetime(2026, 9, 28, tzinfo=UTC)
        assert hi == datetime(2026, 9, 29, tzinfo=UTC)

    def test_ambiguous_time_takes_first_occurrence(self):
        # 2026-10-25 02:30 happens twice in Berlin; fold=0 → CEST (+2).
        got = local_instant(date(2026, 10, 25), hm("02:30"), "Europe/Berlin")
        assert got == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


class TestParseEventBound:
    """API calendar bounds normalise to tz-aware UTC instants, so the
    ``start_dt`` / ``end_dt`` columns never hold a date-only or offset-less
    value (one such row 500'd the whole space calendar)."""

    def test_date_only_start_is_local_midnight_in_event_tz(self):
        got = parse_event_bound("2026-10-20", tz="Europe/Zurich", is_end=False)
        assert got == datetime(2026, 10, 19, 22, 0, tzinfo=UTC)

    def test_date_only_end_includes_that_whole_day(self):
        got = parse_event_bound("2026-10-20", tz="Europe/Zurich", is_end=True)
        assert got == datetime(2026, 10, 20, 22, 0, tzinfo=UTC)

    def test_date_only_in_utc(self):
        assert parse_event_bound("2026-10-20", tz="UTC", is_end=False) == datetime(
            2026, 10, 20, tzinfo=UTC
        )
        assert parse_event_bound("2026-10-20", tz="UTC", is_end=True) == datetime(
            2026, 10, 21, tzinfo=UTC
        )

    def test_offsetless_datetime_is_utc(self):
        got = parse_event_bound("2026-10-20T10:00:00", tz="Europe/Zurich", is_end=False)
        assert got == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

    def test_offset_is_converted_to_utc(self):
        got = parse_event_bound("2026-10-20T10:00:00+02:00", tz="UTC", is_end=False)
        assert got == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
        assert got.utcoffset() == timedelta(0)

    def test_z_suffix(self):
        got = parse_event_bound("2026-10-20T10:00:00Z", tz="UTC", is_end=True)
        assert got == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

    @pytest.mark.parametrize("bad", ["", "tomorrow", "2026-13-01", "2026-10-20T25:00"])
    def test_malformed_raises_value_error(self, bad):
        with pytest.raises(ValueError, match="invalid datetime"):
            parse_event_bound(bad, tz="UTC", is_end=False)

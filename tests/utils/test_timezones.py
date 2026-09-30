"""Shared IANA timezone validation (:mod:`socialhome.utils.timezones`)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone

import pytest

from socialhome.utils.timezones import (
    DEFAULT_TZ,
    coerce_tz,
    is_valid_tz,
    local_date,
    local_instant,
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
    assert zone_of("Mars/Base") is timezone.utc
    late = datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc)
    assert local_date(late, "Europe/Berlin") == date(2026, 9, 28)
    assert local_date(late, "UTC") == date(2026, 9, 27)


class TestLocalInstant:
    def test_berlin_summer_is_utc_plus_two(self):
        got = local_instant(date(2026, 9, 28), hm("08:00"), "Europe/Berlin")
        assert got == datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)
        assert got.tzinfo is timezone.utc

    def test_unknown_zone_falls_back_to_utc(self):
        got = local_instant(date(2026, 9, 28), hm("08:00"), "Mars/Base")
        assert got == datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)

    def test_nonexistent_time_shifts_forward(self):
        # 2026-03-29 02:30 doesn't exist in Berlin (02:00 → 03:00): it
        # lands on 03:30 CEST, i.e. 01:30 UTC.
        got = local_instant(date(2026, 3, 29), hm("02:30"), "Europe/Berlin")
        assert got == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)

    def test_ambiguous_time_takes_first_occurrence(self):
        # 2026-10-25 02:30 happens twice in Berlin; fold=0 → CEST (+2).
        got = local_instant(date(2026, 10, 25), hm("02:30"), "Europe/Berlin")
        assert got == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)

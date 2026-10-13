"""Tests for the dashboard's household wall clock (time + long date)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.clock import (
    WallClock,
    ago,
    days_late,
    due_label,
    hhmm,
    household_clock,
    relative_in,
    resolve_zone,
    until,
)
from socialhome.i18n import Catalog

CATALOG = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
#: 2027-01-15 08:00:00 UTC — a Friday.
NOW = 1_800_000_000.0


def _t(locale: str):
    def t(key: str, **fmt: object) -> str:
        return CATALOG.gettext(key, locale=locale, **fmt)

    return t


def test_resolve_zone_known():
    assert resolve_zone("Europe/Berlin") == ZoneInfo("Europe/Berlin")


@pytest.mark.parametrize("name", ["Not/AZone", "", "../../etc/passwd", "\x00", 5])
def test_resolve_zone_falls_back_to_utc(name):
    assert resolve_zone(name) is UTC  # type: ignore[arg-type]


def test_household_clock_in_zone():
    clock = household_clock(NOW, ZoneInfo("Europe/Berlin"), _t("en"))
    assert clock == WallClock(
        time="09:00",
        date="Friday, January 15",
        day="2027-01-15",
        iso="2027-01-15T09:00:00+01:00",
        tz="Europe/Berlin",
        unix=1_800_000_000,
    )


def test_household_clock_utc():
    clock = household_clock(NOW, UTC, _t("en"))
    assert clock.time == "08:00"
    assert clock.tz == "UTC"
    assert clock.iso == "2027-01-15T08:00:00+00:00"


def test_household_clock_crosses_midnight_by_zone():
    # 08:00 UTC is already the next day in Kiritimati (UTC+14).
    clock = household_clock(
        datetime(2027, 1, 15, 12, 30, tzinfo=UTC).timestamp(),
        ZoneInfo("Pacific/Kiritimati"),
        _t("en"),
    )
    assert clock.day == "2027-01-16"
    assert clock.date == "Saturday, January 16"
    assert clock.time == "02:30"


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "Friday, January 15"),
        ("de", "Freitag, 15. Januar"),
        ("es", "viernes, 15 de enero"),
        ("fr", "vendredi 15 janvier"),
        ("nl", "vrijdag 15 januari"),
    ],
)
def test_long_date_per_locale(locale, expected):
    assert household_clock(NOW, UTC, _t(locale)).date == expected


@pytest.mark.parametrize("month", range(1, 13))
def test_every_month_has_a_name(month):
    ts = datetime(2027, month, 1, 12, tzinfo=UTC).timestamp()
    date = household_clock(ts, UTC, _t("en")).date
    assert "dashboard." not in date


@pytest.mark.parametrize("day", range(4, 11))
def test_every_weekday_has_a_name(day):
    ts = datetime(2027, 1, day, 12, tzinfo=UTC).timestamp()
    date = household_clock(ts, UTC, _t("de")).date
    assert "dashboard." not in date


# ─── Times and relative phrases ──────────────────────────────────────────

BERLIN = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 10, 12, 14, 32, 10, tzinfo=BERLIN)


def test_hhmm_is_24_hour_in_the_values_own_zone():
    assert hhmm(datetime(2026, 10, 12, 7, 5, tzinfo=BERLIN)) == "07:05"
    assert hhmm(datetime(2026, 10, 12, 21, 0, tzinfo=BERLIN)) == "21:00"


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=-5), "now"),
        (timedelta(0), "now"),
        (timedelta(seconds=20), "in 1 min"),
        (timedelta(minutes=28), "in 28 min"),
        (timedelta(minutes=59, seconds=30), "in 1 h"),
        (timedelta(minutes=60), "in 1 h"),
        (timedelta(minutes=95), "in 1 h 35 min"),
        (timedelta(hours=2, minutes=59), "in 2 h 59 min"),
        (timedelta(hours=4, minutes=10), "in 4 h"),
    ],
)
def test_relative_in(delta, expected):
    assert relative_in(T0, T0 + delta, _t("en")) == expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=-30), "just now"),  # a tablet clock a bit behind
        (timedelta(seconds=59), "just now"),
        (timedelta(minutes=1), "1 min ago"),
        (timedelta(minutes=4, seconds=50), "4 min ago"),
    ],
)
def test_ago(delta, expected):
    assert ago(T0 + delta, T0, _t("en")) == expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(minutes=59, seconds=59), "59 min ago"),
        (timedelta(minutes=60), "1 h ago"),
        (timedelta(hours=2, minutes=59), "2 h ago"),
        (timedelta(hours=23, minutes=10), "23 h ago"),
    ],
)
def test_ago_in_hours(delta, expected):
    """The Map tab's "last seen 2 h ago": whole hours from an hour on;
    without ``hours`` minutes keep counting (the chat's phrase)."""
    assert ago(T0 + delta, T0, _t("en"), hours=True) == expected
    assert ago(T0 + timedelta(minutes=90), T0, _t("en")) == "90 min ago"


def test_ago_is_localised():
    assert ago(T0 + timedelta(hours=2), T0, _t("de"), hours=True) == "vor 2 Std."
    assert ago(T0 + timedelta(minutes=4), T0, _t("de")) == "vor 4 Min."


def test_relative_in_is_localised():
    assert relative_in(T0, T0 + timedelta(minutes=28), _t("de")) == "in 28 Min."
    assert relative_in(T0, T0 + timedelta(minutes=95), _t("nl")) == "over 1 uur 35 min"


def test_until():
    end = datetime(2026, 10, 12, 17, 0, tzinfo=BERLIN)
    assert until(end, _t("en")) == "until 17:00"
    assert until(end, _t("fr")) == "jusqu'à 17:00"


@pytest.mark.parametrize(
    ("days", "expected"), [(1, "1 day late"), (2, "2 days late"), (10, "10 days late")]
)
def test_days_late(days, expected):
    assert days_late(days, _t("en")) == expected


def test_days_late_is_localised():
    assert days_late(2, _t("de")) == "2 Tage überfällig"


def test_due_label():
    today = date(2026, 10, 12)  # a Monday
    t = _t("en")
    assert due_label(today, today, t) == "today"
    assert due_label(today + timedelta(days=1), today, t) == "tomorrow"
    assert due_label(today + timedelta(days=3), today, t) == "Thursday"
    assert due_label(date(2026, 10, 30), today, t) == "October 30"
    assert due_label(None, today, t) is None
    assert due_label(today + timedelta(days=3), today, _t("de")) == "Donnerstag"

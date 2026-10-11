"""Tests for the dashboard's household wall clock (time + long date)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.clock import WallClock, household_clock, resolve_zone
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

"""Coverage fill for :mod:`social_home.utils.rrule`."""

from __future__ import annotations

from datetime import datetime, timezone

from social_home.utils.rrule import _parse_until, expand_rrule, parse_rrule


def _dt(y, m, d, h=0, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# ── _parse_until ──────────────────────────────────────────────────────


def test_parse_until_empty_string():
    assert _parse_until("") is None


def test_parse_until_compact_form():
    assert _parse_until("20260515T080000Z") == _dt(2026, 5, 15, 8, 0)


def test_parse_until_compact_bad_format_returns_none():
    assert _parse_until("20260515T99FFFFZ") is None


def test_parse_until_yyyymmdd_only():
    assert _parse_until("20260515") == _dt(2026, 5, 15)


def test_parse_until_yyyymmdd_invalid_returns_none():
    assert _parse_until("20260230") is None  # Feb 30 impossible


def test_parse_until_iso_with_z():
    assert _parse_until("2026-05-15T08:00:00Z") == _dt(2026, 5, 15, 8, 0)


def test_parse_until_totally_bogus_returns_none():
    assert _parse_until("not-a-date") is None


# ── parse_rrule ───────────────────────────────────────────────────────


def test_parse_rrule_empty_returns_defaults():
    out = parse_rrule("")
    assert out["FREQ"] is None
    assert out["INTERVAL"] == 1
    assert out["BYDAY"] == []
    assert out["COUNT"] is None
    assert out["UNTIL"] is None


def test_parse_rrule_skips_chunks_without_equals():
    out = parse_rrule("garbage;FREQ=DAILY")
    assert out["FREQ"] == "DAILY"


def test_parse_rrule_unknown_freq_becomes_none():
    out = parse_rrule("FREQ=HOURLY")
    assert out["FREQ"] is None


def test_parse_rrule_bad_interval_ignored():
    out = parse_rrule("FREQ=DAILY;INTERVAL=abc")
    assert out["INTERVAL"] == 1


def test_parse_rrule_byday_filters_unknown():
    out = parse_rrule("FREQ=WEEKLY;BYDAY=MO,QQ,FR")
    assert out["BYDAY"] == ["MO", "FR"]


def test_parse_rrule_bad_count_ignored():
    out = parse_rrule("FREQ=DAILY;COUNT=abc")
    assert out["COUNT"] is None


def test_parse_rrule_count_clamped_to_zero():
    out = parse_rrule("FREQ=DAILY;COUNT=-5")
    assert out["COUNT"] == 0


def test_parse_rrule_until_parsed():
    out = parse_rrule("FREQ=DAILY;UNTIL=20260101T000000Z")
    assert out["UNTIL"] == _dt(2026, 1, 1)


def test_parse_rrule_until_bogus_ignored():
    out = parse_rrule("FREQ=DAILY;UNTIL=not-a-date")
    assert out["UNTIL"] is None


# ── expand_rrule: non-recurring ───────────────────────────────────────


def test_expand_no_rrule_in_window():
    out = expand_rrule(
        _dt(2026, 1, 1),
        _dt(2026, 1, 1, 1),
        None,
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2026, 1, 2),
    )
    assert len(out) == 1


def test_expand_no_rrule_outside_window():
    out = expand_rrule(
        _dt(2026, 1, 1),
        _dt(2026, 1, 1, 1),
        None,
        window_start=_dt(2026, 6, 1),
        window_end=_dt(2026, 7, 1),
    )
    assert out == []


# ── DAILY ─────────────────────────────────────────────────────────────


def test_expand_daily():
    out = expand_rrule(
        _dt(2026, 1, 1),
        _dt(2026, 1, 1, 1),
        "FREQ=DAILY",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2026, 1, 4),
    )
    assert len(out) == 3


def test_expand_daily_count_stops_emission():
    out = expand_rrule(
        _dt(2026, 1, 1),
        _dt(2026, 1, 1, 1),
        "FREQ=DAILY;COUNT=3",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2026, 1, 31),
    )
    assert len(out) == 3


def test_expand_daily_until_stops_emission():
    out = expand_rrule(
        _dt(2026, 1, 1),
        _dt(2026, 1, 1, 1),
        "FREQ=DAILY;UNTIL=20260103T000000Z",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2026, 1, 10),
    )
    assert len(out) == 3


# ── WEEKLY ────────────────────────────────────────────────────────────


def test_expand_weekly_default_uses_seed_weekday():
    # 2026-01-01 is a Thursday.
    out = expand_rrule(
        _dt(2026, 1, 1, 10),
        _dt(2026, 1, 1, 11),
        "FREQ=WEEKLY;COUNT=3",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2026, 2, 1),
    )
    assert len(out) == 3
    assert out[0][0].weekday() == 3  # Thursday


def test_expand_weekly_byday():
    out = expand_rrule(
        _dt(2026, 1, 5, 9),  # Mon
        _dt(2026, 1, 5, 10),
        "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2026, 2, 1),
    )
    assert len(out) == 4


def test_expand_weekly_byday_skips_days_before_seed():
    # BYDAY may include MO but seed starts later in the week.
    out = expand_rrule(
        _dt(2026, 1, 7, 9),  # Wed
        _dt(2026, 1, 7, 10),
        "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=3",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2026, 2, 1),
    )
    # Should not include the MO before the seed on week 1.
    for start, _ in out:
        assert start >= _dt(2026, 1, 7, 9)


# ── MONTHLY ───────────────────────────────────────────────────────────


def test_expand_monthly():
    out = expand_rrule(
        _dt(2026, 1, 15),
        _dt(2026, 1, 15, 1),
        "FREQ=MONTHLY;COUNT=3",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2027, 1, 1),
    )
    assert len(out) == 3


def test_expand_monthly_handles_feb_short_month():
    # Jan 31 → Feb has no 31st, fallback to Feb 28.
    out = expand_rrule(
        _dt(2025, 1, 31),  # 2025 is non-leap
        _dt(2025, 1, 31, 1),
        "FREQ=MONTHLY;COUNT=3",
        window_start=_dt(2025, 1, 1),
        window_end=_dt(2026, 1, 1),
    )
    assert len(out) == 3


# ── YEARLY ────────────────────────────────────────────────────────────


def test_expand_yearly():
    out = expand_rrule(
        _dt(2026, 5, 1),
        _dt(2026, 5, 1, 1),
        "FREQ=YEARLY;COUNT=3",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2030, 1, 1),
    )
    assert len(out) == 3


def test_expand_yearly_feb29_falls_back_to_feb28():
    out = expand_rrule(
        _dt(2024, 2, 29),  # leap
        _dt(2024, 2, 29, 1),
        "FREQ=YEARLY;COUNT=3",
        window_start=_dt(2024, 1, 1),
        window_end=_dt(2030, 1, 1),
    )
    # 2024 (leap), 2025 (feb 28), 2026 (feb 28)
    assert len(out) == 3


def test_expand_unknown_freq_treats_as_single_occurrence():
    # "FREQ=HOURLY" is dropped → seed only.
    out = expand_rrule(
        _dt(2026, 1, 1),
        _dt(2026, 1, 1, 1),
        "FREQ=HOURLY",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2026, 1, 2),
    )
    assert len(out) == 1


def test_expand_safety_cap():
    """safety_cap prevents runaway loops."""
    out = expand_rrule(
        _dt(2026, 1, 1),
        _dt(2026, 1, 1, 1),
        "FREQ=DAILY",
        window_start=_dt(2026, 1, 1),
        window_end=_dt(2100, 1, 1),
        safety_cap=5,
    )
    # Only 5 emissions allowed.
    assert len(out) == 5

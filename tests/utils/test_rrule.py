"""RRULE expansion — RFC 5545 subset (§17.2)."""

from __future__ import annotations

from datetime import UTC, datetime

from socialhome.utils.rrule import expand_rrule, parse_rrule


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


def test_parse_basic_freq():
    out = parse_rrule("FREQ=DAILY;INTERVAL=2")
    assert out["FREQ"] == "DAILY"
    assert out["INTERVAL"] == 2


def test_parse_unknown_freq_returns_none():
    out = parse_rrule("FREQ=SECONDLY")
    assert out["FREQ"] is None


def test_parse_byday_list():
    out = parse_rrule("FREQ=WEEKLY;BYDAY=MO,WE,FR")
    assert out["BYDAY"] == ["MO", "WE", "FR"]


def test_empty_rrule_returns_only_seed_in_window():
    seed_s = _dt("2026-04-10T09:00:00")
    seed_e = _dt("2026-04-10T10:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        None,
        window_start=_dt("2026-04-01T00:00:00"),
        window_end=_dt("2026-05-01T00:00:00"),
    )
    assert occs == [(seed_s, seed_e)]


def test_empty_rrule_seed_outside_window_returns_empty():
    seed_s = _dt("2025-01-01T00:00:00")
    seed_e = _dt("2025-01-01T01:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        None,
        window_start=_dt("2026-04-01T00:00:00"),
        window_end=_dt("2026-05-01T00:00:00"),
    )
    assert occs == []


def test_daily_generates_five_days():
    seed_s = _dt("2026-04-10T09:00:00")
    seed_e = _dt("2026-04-10T10:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=DAILY",
        window_start=_dt("2026-04-10T00:00:00"),
        window_end=_dt("2026-04-15T00:00:00"),
    )
    assert len(occs) == 5
    assert occs[0][0] == seed_s
    assert occs[4][0] == _dt("2026-04-14T09:00:00")


def test_daily_with_interval_2_skips_every_other():
    seed_s = _dt("2026-04-10T09:00:00")
    seed_e = _dt("2026-04-10T10:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=DAILY;INTERVAL=2",
        window_start=_dt("2026-04-10T00:00:00"),
        window_end=_dt("2026-04-20T00:00:00"),
    )
    # Every other day over 10 days = 5 occurrences.
    assert [s.day for s, _ in occs] == [10, 12, 14, 16, 18]


def test_weekly_byday_monday_wednesday():
    # 2026-04-06 is Monday.
    seed_s = _dt("2026-04-06T09:00:00")
    seed_e = _dt("2026-04-06T10:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=WEEKLY;BYDAY=MO,WE",
        window_start=_dt("2026-04-06T00:00:00"),
        window_end=_dt("2026-04-20T00:00:00"),
    )
    # Mon/Wed for two weeks = 4 occurrences.
    days = [(s.day, s.weekday()) for s, _ in occs]
    assert days == [(6, 0), (8, 2), (13, 0), (15, 2)]


def test_count_terminator():
    seed_s = _dt("2026-04-10T09:00:00")
    seed_e = _dt("2026-04-10T10:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=DAILY;COUNT=3",
        window_start=_dt("2026-04-10T00:00:00"),
        window_end=_dt("2026-05-01T00:00:00"),
    )
    assert len(occs) == 3


def test_until_terminator():
    seed_s = _dt("2026-04-10T09:00:00")
    seed_e = _dt("2026-04-10T10:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=DAILY;UNTIL=20260413T000000Z",
        window_start=_dt("2026-04-10T00:00:00"),
        window_end=_dt("2026-05-01T00:00:00"),
    )
    assert len(occs) == 3  # 10, 11, 12


def test_monthly_same_day_of_month():
    seed_s = _dt("2026-01-15T09:00:00")
    seed_e = _dt("2026-01-15T10:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=MONTHLY;COUNT=3",
        window_start=_dt("2026-01-01T00:00:00"),
        window_end=_dt("2026-12-31T00:00:00"),
    )
    assert [s.month for s, _ in occs] == [1, 2, 3]


def test_yearly():
    seed_s = _dt("2020-07-04T00:00:00")
    seed_e = _dt("2020-07-04T23:59:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=YEARLY;COUNT=5",
        window_start=_dt("2020-01-01T00:00:00"),
        window_end=_dt("2030-01-01T00:00:00"),
    )
    assert [s.year for s, _ in occs] == [2020, 2021, 2022, 2023, 2024]


def test_window_clamps_even_with_recurring():
    seed_s = _dt("2026-01-01T09:00:00")
    seed_e = _dt("2026-01-01T10:00:00")
    # Recurs daily forever — we stop iterating when we leave the window.
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=DAILY",
        window_start=_dt("2026-04-01T00:00:00"),
        window_end=_dt("2026-04-04T00:00:00"),
    )
    days = [s.day for s, _ in occs]
    assert days == [1, 2, 3]
    assert all(s.month == 4 for s, _ in occs)


# ── DST correctness — the wall-clock-anchored ``tz=`` path ──────────────


def test_weekly_recurrence_preserves_wall_clock_across_dst():
    """A "weekly Tuesday 19:00 Europe/Berlin" event must stay at 19:00
    Berlin across the spring-forward DST transition (last Sunday of
    March in Europe/Berlin). Without ``tz=`` the legacy expander drifts
    by ±1 h after the shift — with the IANA anchor it converges
    correctly.

    The seed is March 24 2026 19:00 Berlin (UTC=18:00, winter); the
    occurrence after DST (March 31 2026) must be 19:00 Berlin which is
    UTC=17:00 because Berlin is now CEST (UTC+2).
    """
    # Seed expressed as UTC instant: 19:00 Berlin in winter = 18:00 UTC.
    seed_s = _dt("2026-03-24T18:00:00")
    seed_e = _dt("2026-03-24T20:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=WEEKLY;COUNT=3",
        window_start=_dt("2026-03-01T00:00:00"),
        window_end=_dt("2026-04-30T00:00:00"),
        tz="Europe/Berlin",
    )
    # Three occurrences: Mar 24 (winter), Mar 31 (after DST), Apr 7.
    assert len(occs) == 3
    # Pre-DST: 19:00 Berlin = 18:00Z.
    assert occs[0][0].isoformat() == "2026-03-24T18:00:00+00:00"
    # Post-DST: 19:00 Berlin = 17:00Z — this is the bug the fix exists for.
    assert occs[1][0].isoformat() == "2026-03-31T17:00:00+00:00"
    assert occs[2][0].isoformat() == "2026-04-07T17:00:00+00:00"


def test_daily_recurrence_preserves_wall_clock_across_dst():
    """Daily 09:00 New York event must stay at 09:00 across the
    fall-back transition (first Sunday of November in America/New_York,
    Nov 2 2025: clocks roll 02:00→01:00, switching from EDT to EST)."""
    # Nov 1 09:00 New York = 13:00Z (EDT, UTC-4).
    seed_s = _dt("2025-11-01T13:00:00")
    seed_e = _dt("2025-11-01T14:00:00")
    occs = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=DAILY;COUNT=4",
        window_start=_dt("2025-10-25T00:00:00"),
        window_end=_dt("2025-11-10T00:00:00"),
        tz="America/New_York",
    )
    assert len(occs) == 4
    # Pre-DST (Nov 1): 09:00 NY = 13:00Z.
    assert occs[0][0].isoformat() == "2025-11-01T13:00:00+00:00"
    # On the fall-back day itself (Nov 2) — 09:00 NY is already EST=14:00Z.
    assert occs[1][0].isoformat() == "2025-11-02T14:00:00+00:00"
    # Post-DST: 09:00 NY = 14:00Z (EST, UTC-5).
    assert occs[2][0].isoformat() == "2025-11-03T14:00:00+00:00"
    assert occs[3][0].isoformat() == "2025-11-04T14:00:00+00:00"


def test_weekly_with_tz_unchanged_for_utc_anchor():
    """When the anchor zone is UTC the new ``tz=`` path produces the
    exact same occurrences as the legacy path — regression guard so
    we didn't accidentally change behaviour for UTC-anchored events.
    """
    seed_s = _dt("2026-06-02T09:00:00")
    seed_e = _dt("2026-06-02T10:00:00")
    legacy = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=WEEKLY;COUNT=3",
        window_start=_dt("2026-06-01T00:00:00"),
        window_end=_dt("2026-07-01T00:00:00"),
    )
    via_tz = expand_rrule(
        seed_s,
        seed_e,
        "FREQ=WEEKLY;COUNT=3",
        window_start=_dt("2026-06-01T00:00:00"),
        window_end=_dt("2026-07-01T00:00:00"),
        tz="UTC",
    )
    assert legacy == via_tz

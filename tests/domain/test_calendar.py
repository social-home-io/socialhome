"""Tests for socialhome.domain.calendar — Calendar, CalendarEvent, and related types."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from socialhome.domain.calendar import (
    Calendar,
    CalendarEvent,
    CalendarEventCopy,
    CalendarEventCreate,
    CalendarEventUpdate,
    CalendarRSVP,
    RSVPStatus,
    all_day_covers,
)


def test_calendar_construction():
    """Calendar can be constructed with all required fields."""
    cal = Calendar(
        id="cal-1",
        name="My Calendar",
        color="#4a90e2",
        owner_username="alice",
    )
    assert cal.id == "cal-1"
    assert cal.name == "My Calendar"
    assert cal.color == "#4a90e2"
    assert cal.owner_username == "alice"
    assert cal.calendar_type == "personal"


def test_calendar_space_type():
    """Calendar calendar_type can be set to 'space'."""
    cal = Calendar(
        id="cal-2",
        name="Space Calendar",
        color="#ff0000",
        owner_username="space-owner",
        calendar_type="space",
    )
    assert cal.calendar_type == "space"


def test_calendar_is_frozen():
    """Calendar is immutable (frozen=True)."""
    cal = Calendar(id="c", name="n", color="#fff", owner_username="u")
    with pytest.raises((AttributeError, TypeError)):
        cal.name = "changed"  # type: ignore[misc]


def test_calendar_event_construction():
    """CalendarEvent can be constructed with required fields and defaults."""
    now = datetime.now(timezone.utc)
    evt = CalendarEvent(
        id="evt-1",
        calendar_id="cal-1",
        summary="Meeting",
        start=now,
        end=now,
        created_by="uid-alice",
    )
    assert evt.id == "evt-1"
    assert evt.summary == "Meeting"
    assert evt.description is None
    assert evt.all_day is False
    assert evt.attendees == ()
    assert evt.mirrored_from is None


def test_calendar_event_with_optional_fields():
    """CalendarEvent accepts optional fields like description, attendees, mirrored_from."""
    now = datetime.now(timezone.utc)
    evt = CalendarEvent(
        id="evt-2",
        calendar_id="cal-1",
        summary="All-Day",
        start=now,
        end=now,
        created_by="uid-bob",
        description="desc",
        all_day=True,
        attendees=("uid-a", "uid-b"),
        mirrored_from="evt-orig",
    )
    assert evt.all_day is True
    assert evt.attendees == ("uid-a", "uid-b")
    assert evt.mirrored_from == "evt-orig"


def test_calendar_event_create_defaults():
    """CalendarEventCreate has sensible defaults for optional fields."""
    now = datetime.now(timezone.utc)
    create = CalendarEventCreate(summary="New", start=now, end=now)
    assert create.all_day is False
    assert create.description is None
    assert create.attendees == ()


def test_calendar_event_update_all_none():
    """CalendarEventUpdate with no arguments leaves all fields as None."""
    update = CalendarEventUpdate()
    assert update.summary is None
    assert update.description is None
    assert update.start is None
    assert update.end is None
    assert update.all_day is None
    assert update.attendees is None


def test_calendar_event_update_partial():
    """CalendarEventUpdate carries only the fields provided."""
    now = datetime.now(timezone.utc)
    update = CalendarEventUpdate(summary="Updated", start=now)
    assert update.summary == "Updated"
    assert update.start == now
    assert update.end is None


def test_rsvp_status_constants():
    """RSVPStatus exposes the user-settable trio + the host-driven
    REQUESTED / WAITLIST states (Phase C). USER_SETTABLE is the subset
    a member can choose directly; ALL includes everything the schema
    accepts.
    """
    assert RSVPStatus.GOING == "going"
    assert RSVPStatus.MAYBE == "maybe"
    assert RSVPStatus.DECLINED == "declined"
    assert RSVPStatus.REQUESTED == "requested"
    assert RSVPStatus.WAITLIST == "waitlist"
    assert RSVPStatus.USER_SETTABLE == frozenset({"going", "maybe", "declined"})
    assert RSVPStatus.ALL == frozenset(
        {"going", "maybe", "declined", "requested", "waitlist"}
    )


def test_calendar_rsvp_construction():
    """CalendarRSVP can be constructed and fields are accessible."""
    rsvp = CalendarRSVP(
        event_id="evt-1",
        user_id="uid-alice",
        status=RSVPStatus.GOING,
        updated_at="2025-01-01T00:00:00",
    )
    assert rsvp.event_id == "evt-1"
    assert rsvp.status == "going"


def test_calendar_event_copy_construction():
    """CalendarEventCopy carries the sibling row identity triple."""
    copy = CalendarEventCopy(
        event_id="evt-1",
        calendar_id="cal-1",
        owner_username="alice",
    )
    assert copy.event_id == "evt-1"
    assert copy.calendar_id == "cal-1"
    assert copy.owner_username == "alice"


def test_calendar_event_copy_is_frozen():
    """CalendarEventCopy is immutable like every other domain type."""
    copy = CalendarEventCopy(
        event_id="evt-1",
        calendar_id="cal-1",
        owner_username="alice",
    )
    with pytest.raises((AttributeError, TypeError)):
        copy.event_id = "evt-2"  # type: ignore[misc]


# ─── all_day_covers ──────────────────────────────────────────────────────


def _all_day(start: str, end: str, tz: str = "UTC") -> CalendarEvent:
    return CalendarEvent(
        id="e",
        calendar_id="c",
        summary="x",
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        created_by="u",
        all_day=True,
        tz=tz,
    )


def test_all_day_yesterday_ics_event_is_not_today_in_berlin():
    # ICS all-day for 29 Sep: 00:00Z … 00:00Z next day (exclusive end).
    ev = _all_day("2026-09-29T00:00:00+00:00", "2026-09-30T00:00:00+00:00")
    assert all_day_covers(ev, date(2026, 9, 30), "Europe/Berlin") is False
    assert all_day_covers(ev, date(2026, 9, 29), "Europe/Berlin") is True


def test_all_day_tomorrow_is_not_today_west_of_utc():
    ev = _all_day("2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00")
    assert all_day_covers(ev, date(2026, 9, 30), "America/Bogota") is False
    assert all_day_covers(ev, date(2026, 10, 1), "America/Bogota") is True


def test_all_day_multi_day_covers_every_day_in_its_own_zone():
    # SPA shape: 00:00 … 23:59 Berlin, 28 Sep – 2 Oct.
    ev = _all_day(
        "2026-09-27T22:00:00+00:00", "2026-10-02T21:59:00+00:00", "Europe/Berlin"
    )
    assert [all_day_covers(ev, date(2026, 9, d), "UTC") for d in (27, 28, 30)] == [
        False,
        True,
        True,
    ]
    assert all_day_covers(ev, date(2026, 10, 2), "UTC") is True
    assert all_day_covers(ev, date(2026, 10, 3), "UTC") is False


def test_all_day_without_a_zone_uses_the_fallback():
    ev = _all_day("2026-09-29T22:00:00+00:00", "2026-09-30T21:59:00+00:00", tz="")
    assert all_day_covers(ev, date(2026, 9, 30), "Europe/Berlin") is True
    assert all_day_covers(ev, date(2026, 9, 29), "Europe/Berlin") is False
    assert all_day_covers(ev, date(2026, 9, 29), "UTC") is True


def test_all_day_degenerate_end_is_the_start_day():
    ev = _all_day("2026-09-30T00:00:00+00:00", "2026-09-30T00:00:00+00:00")
    assert all_day_covers(ev, date(2026, 9, 30), "UTC") is True

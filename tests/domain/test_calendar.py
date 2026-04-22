"""Tests for socialhome.domain.calendar — Calendar, CalendarEvent, and related types."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.calendar import (
    Calendar,
    CalendarEvent,
    CalendarEventCreate,
    CalendarEventUpdate,
    CalendarRSVP,
    RSVPStatus,
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
    """RSVPStatus exposes GOING, MAYBE, DECLINED and an ALL frozenset."""
    assert RSVPStatus.GOING == "going"
    assert RSVPStatus.MAYBE == "maybe"
    assert RSVPStatus.DECLINED == "declined"
    assert RSVPStatus.ALL == frozenset({"going", "maybe", "declined"})


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

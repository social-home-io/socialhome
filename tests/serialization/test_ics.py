"""Tests for the iCalendar serializer (Phase F)."""

from __future__ import annotations

from datetime import datetime, timezone

from socialhome.domain.calendar import CalendarEvent, EventReminder
from socialhome.serialization.ics import (
    feed_etag,
    serialize_event,
    serialize_feed,
)


def _ev(**kw) -> CalendarEvent:
    defaults = dict(
        id="ev-1",
        calendar_id="sp-1",
        summary="Meeting",
        start=datetime(2030, 1, 5, 10, 0, tzinfo=timezone.utc),
        end=datetime(2030, 1, 5, 11, 0, tzinfo=timezone.utc),
        created_by="uid-alice",
    )
    defaults.update(kw)
    return CalendarEvent(**defaults)


def test_serialize_event_basic_structure():
    payload = serialize_event(_ev()).decode("utf-8")
    assert payload.startswith("BEGIN:VCALENDAR\r\n")
    assert payload.rstrip().endswith("END:VCALENDAR")
    assert "VERSION:2.0" in payload
    assert "PRODID:-//Social Home//Calendar//EN" in payload
    assert "BEGIN:VEVENT" in payload
    assert "END:VEVENT" in payload
    assert "UID:ev-1" in payload
    assert "DTSTART:20300105T100000Z" in payload
    assert "DTEND:20300105T110000Z" in payload
    assert "SUMMARY:Meeting" in payload


def test_serialize_event_escapes_special_chars():
    payload = serialize_event(
        _ev(summary="with, semi; and\nnewline"),
    ).decode("utf-8")
    # Comma, semicolon, newline all escaped per RFC 5545.
    assert "with\\, semi\\; and\\nnewline" in payload


def test_serialize_event_all_day_emits_value_date():
    payload = serialize_event(
        _ev(
            all_day=True,
            start=datetime(2030, 6, 1, tzinfo=timezone.utc),
            end=datetime(2030, 6, 2, tzinfo=timezone.utc),
        ),
    ).decode("utf-8")
    assert "DTSTART;VALUE=DATE:20300601" in payload
    assert "DTEND;VALUE=DATE:20300602" in payload


def test_serialize_event_recurring_passes_rrule_through():
    payload = serialize_event(
        _ev(rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR"),
    ).decode("utf-8")
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR" in payload


def test_serialize_event_includes_valarm_for_reminders():
    payload = serialize_event(
        _ev(),
        reminders=[
            EventReminder(
                event_id="ev-1",
                user_id="uid-alice",
                occurrence_at="2030-01-05T10:00:00+00:00",
                minutes_before=60,
                fire_at="2030-01-05T09:00:00+00:00",
            ),
            EventReminder(
                event_id="ev-1",
                user_id="uid-alice",
                occurrence_at="2030-01-05T10:00:00+00:00",
                minutes_before=15,
                fire_at="2030-01-05T09:45:00+00:00",
            ),
        ],
    ).decode("utf-8")
    assert payload.count("BEGIN:VALARM") == 2
    assert "TRIGGER:-PT60M" in payload
    assert "TRIGGER:-PT15M" in payload


def test_serialize_event_cancelled_status():
    payload = serialize_event(_ev(), cancelled=True).decode("utf-8")
    assert "STATUS:CANCELLED" in payload


def test_serialize_feed_emits_multiple_vevents():
    a = _ev(id="a", summary="First")
    b = _ev(
        id="b",
        summary="Second",
        start=datetime(2030, 2, 1, 10, 0, tzinfo=timezone.utc),
        end=datetime(2030, 2, 1, 11, 0, tzinfo=timezone.utc),
    )
    payload = serialize_feed([a, b]).decode("utf-8")
    assert payload.count("BEGIN:VEVENT") == 2
    assert "UID:a" in payload
    assert "UID:b" in payload


def test_feed_etag_changes_with_payload():
    a = serialize_feed([_ev(id="x")])
    b = serialize_feed([_ev(id="y")])
    assert feed_etag(a) != feed_etag(b)


def test_feed_etag_is_stable():
    a = serialize_feed([_ev(id="x")])
    assert feed_etag(a) == feed_etag(a)


def test_long_summary_is_folded():
    long_summary = "x" * 200
    payload = serialize_event(_ev(summary=long_summary)).decode("utf-8")
    # No raw line should exceed 75 octets — every continuation starts
    # with a space.
    for line in payload.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75

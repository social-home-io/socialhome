"""Tests for CalendarImportService."""

from __future__ import annotations

import pytest

from socialhome.services.calendar_import_service import (
    AICalendarImportError,
    AICalendarImportParseError,
    AICalendarImportUnavailable,
    CalendarImportService,
    ics_import_key,
)
from socialhome.services.calendar_service import _clean_client_event_uuid


# ─── Fakes ────────────────────────────────────────────────────────────────


class _ScriptedAdapter:
    """Adapter whose ``generate_ai_data`` returns a pre-set reply."""

    def __init__(self, reply: str = ""):
        self._reply = reply
        self.received: dict | None = None

    async def generate_ai_data(self, *, task_name, instructions):
        self.received = {"task_name": task_name, "instructions": instructions}
        return self._reply


class _AdapterWithoutAi:
    pass


class _AdapterRaises:
    async def generate_ai_data(self, *, task_name, instructions):
        raise NotImplementedError("not in this build")


# ─── Fixtures ─────────────────────────────────────────────────────────────

_ICS_SINGLE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-1@test
SUMMARY:School concert
DTSTART:20260512T160000Z
DTEND:20260512T180000Z
DESCRIPTION:Spring program
LOCATION:Main Auditorium
END:VEVENT
END:VCALENDAR
"""

_ICS_MULTI = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:a@test
SUMMARY:Event A
DTSTART:20260601T090000Z
DTEND:20260601T100000Z
END:VEVENT
BEGIN:VEVENT
UID:b@test
SUMMARY:Event B
DTSTART:20260602T140000Z
DTEND:20260602T150000Z
END:VEVENT
END:VCALENDAR
"""

_ICS_ALL_DAY = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:allday@test
SUMMARY:Holiday
DTSTART;VALUE=DATE:20260701
DTEND;VALUE=DATE:20260702
END:VEVENT
END:VCALENDAR
"""

_AI_REPLY_WITH_PROSE = """
Sure! Here is the calendar you asked for:

BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:xyz@test
SUMMARY:Birthday party
DTSTART:20260601T150000Z
DTEND:20260601T170000Z
END:VEVENT
END:VCALENDAR

Hope that helps!
"""


# ─── import_ics ──────────────────────────────────────────────────────────


async def test_import_ics_single_vevent_parses_summary_and_times():
    svc = CalendarImportService(_AdapterWithoutAi())
    events = await svc.import_ics(ics_bytes=_ICS_SINGLE)
    assert len(events) == 1
    assert events[0].summary == "School concert"
    assert not events[0].all_day
    assert events[0].description == "Spring program"
    assert events[0].location == "Main Auditorium"


async def test_import_ics_multi_vevent_returns_all_events():
    svc = CalendarImportService(_AdapterWithoutAi())
    events = await svc.import_ics(ics_bytes=_ICS_MULTI)
    assert [e.summary for e in events] == ["Event A", "Event B"]


async def test_import_ics_all_day_flag_set_when_date_only():
    svc = CalendarImportService(_AdapterWithoutAi())
    events = await svc.import_ics(ics_bytes=_ICS_ALL_DAY)
    assert len(events) == 1
    assert events[0].all_day is True


async def test_import_ics_empty_bytes_raises():
    svc = CalendarImportService(_AdapterWithoutAi())
    with pytest.raises(AICalendarImportError):
        await svc.import_ics(ics_bytes=b"")


async def test_import_ics_oversize_raises():
    svc = CalendarImportService(_AdapterWithoutAi(), max_ics_bytes=10)
    with pytest.raises(AICalendarImportError):
        await svc.import_ics(ics_bytes=b"x" * 100)


async def test_import_ics_malformed_raises_parse_error():
    svc = CalendarImportService(_AdapterWithoutAi())
    with pytest.raises(AICalendarImportParseError):
        await svc.import_ics(ics_bytes=b"totally not a calendar")


async def test_import_ics_no_vevent_raises_parse_error():
    empty_cal = b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//T//EN\nEND:VCALENDAR\n"
    svc = CalendarImportService(_AdapterWithoutAi())
    with pytest.raises(AICalendarImportParseError):
        await svc.import_ics(ics_bytes=empty_cal)


# ─── import_from_image ───────────────────────────────────────────────────


async def test_import_from_image_happy_path():
    adapter = _ScriptedAdapter(_ICS_SINGLE.decode())
    svc = CalendarImportService(adapter)
    events = await svc.import_from_image(
        image_bytes=b"\xff\xd8\xff\xe0",
        mime_type="image/jpeg",
        locale="en",
        caption="concert poster",
    )
    assert len(events) == 1
    assert events[0].summary == "School concert"
    # Prompt carries locale, caption, and the data URL.
    assert "User locale: en" in adapter.received["instructions"]
    assert "concert poster" in adapter.received["instructions"]
    assert "data:image/jpeg;base64," in adapter.received["instructions"]


async def test_import_from_image_tolerates_prose_around_vcalendar():
    svc = CalendarImportService(_ScriptedAdapter(_AI_REPLY_WITH_PROSE))
    events = await svc.import_from_image(image_bytes=b"img", mime_type="image/png")
    assert events[0].summary == "Birthday party"


async def test_import_from_image_empty_reply_parse_error():
    svc = CalendarImportService(_ScriptedAdapter(""))
    with pytest.raises(AICalendarImportParseError):
        await svc.import_from_image(image_bytes=b"img")


async def test_import_from_image_no_vcalendar_parse_error():
    svc = CalendarImportService(_ScriptedAdapter("just some prose"))
    with pytest.raises(AICalendarImportParseError):
        await svc.import_from_image(image_bytes=b"img")


async def test_import_from_image_adapter_without_method_raises_unavailable():
    svc = CalendarImportService(_AdapterWithoutAi())
    with pytest.raises(AICalendarImportUnavailable):
        await svc.import_from_image(image_bytes=b"img")


async def test_import_from_image_not_implemented_raises_unavailable():
    svc = CalendarImportService(_AdapterRaises())
    with pytest.raises(AICalendarImportUnavailable):
        await svc.import_from_image(image_bytes=b"img")


async def test_import_from_image_empty_bytes_raises():
    svc = CalendarImportService(_ScriptedAdapter(_ICS_SINGLE.decode()))
    with pytest.raises(AICalendarImportError):
        await svc.import_from_image(image_bytes=b"")


async def test_import_from_image_oversize_raises():
    svc = CalendarImportService(
        _ScriptedAdapter(_ICS_SINGLE.decode()),
        max_image_bytes=10,
    )
    with pytest.raises(AICalendarImportError):
        await svc.import_from_image(image_bytes=b"x" * 100)


# ─── import_from_prompt ──────────────────────────────────────────────────


async def test_import_from_prompt_happy_path_passes_text_through():
    adapter = _ScriptedAdapter(_ICS_SINGLE.decode())
    svc = CalendarImportService(adapter)
    events = await svc.import_from_prompt(
        prompt="school concert thursday 6pm",
        locale="de",
    )
    assert events[0].summary == "School concert"
    assert "school concert thursday 6pm" in adapter.received["instructions"]
    assert "User locale: de" in adapter.received["instructions"]


async def test_import_from_prompt_empty_raises():
    svc = CalendarImportService(_ScriptedAdapter(_ICS_SINGLE.decode()))
    with pytest.raises(AICalendarImportError):
        await svc.import_from_prompt(prompt="")


async def test_import_from_prompt_adapter_without_method_raises_unavailable():
    svc = CalendarImportService(_AdapterWithoutAi())
    with pytest.raises(AICalendarImportUnavailable):
        await svc.import_from_prompt(prompt="dentist tomorrow 10am")


async def test_import_from_prompt_empty_reply_parse_error():
    svc = CalendarImportService(_ScriptedAdapter(""))
    with pytest.raises(AICalendarImportParseError):
        await svc.import_from_prompt(prompt="x")


# ─── Stable per-VEVENT import key (re-import updates, not duplicates) ────

_ICS_SERIES_WITH_OVERRIDE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:series@test
SUMMARY:Weekly standup
DTSTART:20260601T090000Z
DTEND:20260601T091500Z
RRULE:FREQ=WEEKLY;COUNT=4
END:VEVENT
BEGIN:VEVENT
UID:series@test
RECURRENCE-ID:20260608T090000Z
SUMMARY:Weekly standup (moved)
DTSTART:20260608T100000Z
DTEND:20260608T101500Z
END:VEVENT
END:VCALENDAR
"""

_ICS_NO_UID = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
SUMMARY:No uid here
DTSTART:20260601T090000Z
DTEND:20260601T100000Z
END:VEVENT
END:VCALENDAR
"""


async def test_import_key_is_stable_for_same_uid():
    svc = CalendarImportService(_AdapterWithoutAi())
    first = await svc.import_ics(ics_bytes=_ICS_SINGLE)
    second = await svc.import_ics(ics_bytes=_ICS_SINGLE)
    assert first[0].client_event_uuid is not None
    assert first[0].client_event_uuid == second[0].client_event_uuid
    assert first[0].client_event_uuid == ics_import_key("evt-1@test", None)


async def test_import_key_differs_between_uids():
    svc = CalendarImportService(_AdapterWithoutAi())
    events = await svc.import_ics(ics_bytes=_ICS_MULTI)
    assert events[0].client_event_uuid != events[1].client_event_uuid


async def test_import_key_differs_by_recurrence_id():
    svc = CalendarImportService(_AdapterWithoutAi())
    series, override = await svc.import_ics(ics_bytes=_ICS_SERIES_WITH_OVERRIDE)
    assert series.client_event_uuid == ics_import_key("series@test", None)
    assert override.client_event_uuid == ics_import_key(
        "series@test", "2026-06-08T09:00:00+00:00"
    )
    assert series.client_event_uuid != override.client_event_uuid


async def test_import_key_absent_without_uid():
    svc = CalendarImportService(_AdapterWithoutAi())
    events = await svc.import_ics(ics_bytes=_ICS_NO_UID)
    assert events[0].client_event_uuid is None


def test_import_key_is_32_lowercase_hex_and_passes_uuid_cleaner():
    key = ics_import_key("Mixed-Case-UID@Example.COM", "2026-06-08")
    assert len(key) == 32
    assert key == key.lower()
    int(key, 16)
    assert _clean_client_event_uuid(key) == key


def test_import_key_recurrence_id_none_equals_empty():
    assert ics_import_key("u@t", None) == ics_import_key("u@t", "")
    assert ics_import_key("u@t", None) != ics_import_key("u@t", "2026-06-08")


async def test_import_key_normalises_recurrence_id_shapes():
    """A date RECURRENCE-ID keys on the date; a zoned one keys on UTC, so
    the same occurrence exported with a TZID or as ``Z`` maps to one key."""
    ics = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:d@test
RECURRENCE-ID;VALUE=DATE:20260702
SUMMARY:Holiday moved
DTSTART;VALUE=DATE:20260703
DTEND;VALUE=DATE:20260704
END:VEVENT
BEGIN:VEVENT
UID:z@test
RECURRENCE-ID;TZID=Europe/Berlin:20260608T110000
SUMMARY:Zoned override
DTSTART:20260608T100000Z
DTEND:20260608T110000Z
END:VEVENT
END:VCALENDAR
"""
    svc = CalendarImportService(_AdapterWithoutAi())
    day, zoned = await svc.import_ics(ics_bytes=ics)
    assert day.client_event_uuid == ics_import_key("d@test", "2026-07-02")
    assert zoned.client_event_uuid == ics_import_key(
        "z@test", "2026-06-08T09:00:00+00:00"
    )

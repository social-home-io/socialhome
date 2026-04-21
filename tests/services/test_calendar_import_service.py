"""Tests for CalendarImportService."""

from __future__ import annotations

import pytest

from social_home.services.calendar_import_service import (
    AICalendarImportError,
    AICalendarImportParseError,
    AICalendarImportUnavailable,
    CalendarImportService,
)


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

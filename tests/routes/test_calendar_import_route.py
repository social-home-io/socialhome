"""HTTP tests for /api/calendars/{id}/import_ics, /import_image, /import_prompt."""

from __future__ import annotations

import base64

from socialhome.app_keys import (
    calendar_import_service_key,
    platform_adapter_key,
)
from socialhome.services.calendar_import_service import CalendarImportService

from .conftest import _auth


_ICS_BYTES = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
    b"BEGIN:VEVENT\r\nUID:x@t\r\nSUMMARY:Dentist\r\n"
    b"DTSTART:20260601T100000Z\r\nDTEND:20260601T110000Z\r\n"
    b"END:VEVENT\r\nEND:VCALENDAR\r\n"
)

_AI_REPLY = _ICS_BYTES.decode()


class _FakeAiAdapter:
    """Shim around the real adapter that adds generate_ai_data."""

    def __init__(self, inner, reply: str = _AI_REPLY):
        self._inner = inner
        self._reply = reply

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def generate_ai_data(self, *, task_name, instructions):
        return self._reply


def _swap_import_service(app, adapter):
    app[calendar_import_service_key] = CalendarImportService(adapter)


async def _create_calendar(client):
    r = await client.post(
        "/api/calendars",
        json={"name": "X"},
        headers=_auth(client._tok),
    )
    return (await r.json())["id"]


# ─── /import_ics — always available (no AI needed) ──────────────────────


async def test_import_ics_raw_body_creates_event(client):
    cid = await _create_calendar(client)
    r = await client.post(
        f"/api/calendars/{cid}/import_ics",
        data=_ICS_BYTES,
        headers={**_auth(client._tok), "Content-Type": "text/calendar"},
    )
    assert r.status == 201, await r.text()
    data = await r.json()
    assert len(data["events"]) == 1
    assert data["events"][0]["summary"] == "Dentist"


async def test_import_ics_json_body_creates_event(client):
    cid = await _create_calendar(client)
    r = await client.post(
        f"/api/calendars/{cid}/import_ics",
        json={"ics": _ICS_BYTES.decode()},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    assert len((await r.json())["events"]) == 1


async def test_import_ics_json_missing_field_422(client):
    cid = await _create_calendar(client)
    r = await client.post(
        f"/api/calendars/{cid}/import_ics",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_import_ics_malformed_422(client):
    cid = await _create_calendar(client)
    r = await client.post(
        f"/api/calendars/{cid}/import_ics",
        data=b"not an ics",
        headers={**_auth(client._tok), "Content-Type": "text/calendar"},
    )
    assert r.status == 422


# ─── /import_ics — re-import updates by UID instead of duplicating ──────


def _vcal(*vevents: str) -> bytes:
    body = "".join(f"BEGIN:VEVENT\r\n{v}END:VEVENT\r\n" for v in vevents)
    return (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n{body}END:VCALENDAR\r\n"
    ).encode()


_TWO_EVENTS = (
    "UID:a@t\r\nSUMMARY:Dentist\r\n"
    "DTSTART:20260601T100000Z\r\nDTEND:20260601T110000Z\r\n",
    "UID:b@t\r\nSUMMARY:Piano\r\n"
    "DTSTART:20260602T150000Z\r\nDTEND:20260602T160000Z\r\n",
)


async def _import(client, cid, ics: bytes):
    r = await client.post(
        f"/api/calendars/{cid}/import_ics",
        data=ics,
        headers={**_auth(client._tok), "Content-Type": "text/calendar"},
    )
    assert r.status == 201, await r.text()
    return await r.json()


async def _list(client, cid):
    r = await client.get(
        f"/api/calendars/{cid}/events",
        params={"start": "2026-05-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
        headers=_auth(client._tok),
    )
    assert r.status == 200, await r.text()
    return await r.json()


async def test_import_ics_twice_updates_instead_of_duplicating(client):
    cid = await _create_calendar(client)
    first = await _import(client, cid, _vcal(*_TWO_EVENTS))
    assert first["created"] == 2
    assert first["updated"] == 0

    changed = (_TWO_EVENTS[0].replace("Dentist", "Dentist (moved)"), _TWO_EVENTS[1])
    second = await _import(client, cid, _vcal(*changed))
    assert second["created"] == 0
    assert second["updated"] == 2
    assert {e["id"] for e in second["events"]} == {e["id"] for e in first["events"]}

    rows = await _list(client, cid)
    assert sorted(e["summary"] for e in rows) == ["Dentist (moved)", "Piano"]


async def test_import_ics_series_and_override_stay_separate_rows(client):
    cid = await _create_calendar(client)
    series = (
        "UID:s@t\r\nSUMMARY:Standup\r\n"
        "DTSTART:20260601T090000Z\r\nDTEND:20260601T091500Z\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=4\r\n"
    )
    override = (
        "UID:s@t\r\nRECURRENCE-ID:20260608T090000Z\r\nSUMMARY:Standup moved\r\n"
        "DTSTART:20260608T100000Z\r\nDTEND:20260608T101500Z\r\n"
    )
    first = await _import(client, cid, _vcal(series, override))
    assert first["created"] == 2
    again = await _import(client, cid, _vcal(series, override))
    assert (again["created"], again["updated"]) == (0, 2)
    ids = {e["id"] for e in again["events"]}
    assert len(ids) == 2


async def test_import_ics_without_uid_keeps_creating_new_rows(client):
    cid = await _create_calendar(client)
    no_uid = (
        "SUMMARY:Anonymous\r\nDTSTART:20260601T100000Z\r\nDTEND:20260601T110000Z\r\n"
    )
    await _import(client, cid, _vcal(no_uid))
    second = await _import(client, cid, _vcal(no_uid))
    assert (second["created"], second["updated"]) == (1, 0)
    rows = await _list(client, cid)
    assert [e["summary"] for e in rows] == ["Anonymous", "Anonymous"]


async def test_import_ics_into_two_calendars_one_row_each_same_key(client):
    cid_a = await _create_calendar(client)
    cid_b = await _create_calendar(client)
    a = await _import(client, cid_a, _vcal(_TWO_EVENTS[0]))
    b = await _import(client, cid_b, _vcal(_TWO_EVENTS[0]))
    assert (a["created"], b["created"]) == (1, 1)
    ev_a, ev_b = a["events"][0], b["events"][0]
    assert ev_a["id"] != ev_b["id"]
    assert ev_a["client_event_uuid"] == ev_b["client_event_uuid"]
    assert ev_a["client_event_uuid"] is not None
    assert len(await _list(client, cid_a)) == 1
    assert len(await _list(client, cid_b)) == 1


# ─── /import_image — requires AI ────────────────────────────────────────


async def test_import_image_503_without_ai_adapter(client):
    """Standalone adapter has no generate_ai_data → 503."""
    cid = await _create_calendar(client)
    body = base64.b64encode(b"\x89PNG").decode()
    r = await client.post(
        f"/api/calendars/{cid}/import_image",
        json={"image_data_url": f"data:image/png;base64,{body}"},
        headers=_auth(client._tok),
    )
    assert r.status == 503


async def test_import_image_happy_path_with_ai(client):
    cid = await _create_calendar(client)
    inner = client.app[platform_adapter_key]
    _swap_import_service(client.app, _FakeAiAdapter(inner))
    body = base64.b64encode(b"\x89PNG").decode()
    r = await client.post(
        f"/api/calendars/{cid}/import_image",
        json={"image_data_url": f"data:image/png;base64,{body}", "caption": "c"},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    assert len((await r.json())["events"]) == 1


async def test_import_image_bad_data_url_422(client):
    cid = await _create_calendar(client)
    inner = client.app[platform_adapter_key]
    _swap_import_service(client.app, _FakeAiAdapter(inner))
    r = await client.post(
        f"/api/calendars/{cid}/import_image",
        json={"image_data_url": "not-a-data-url"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_import_image_ai_returns_no_vcalendar_422(client):
    cid = await _create_calendar(client)
    inner = client.app[platform_adapter_key]
    _swap_import_service(client.app, _FakeAiAdapter(inner, reply="just prose"))
    body = base64.b64encode(b"\x89PNG").decode()
    r = await client.post(
        f"/api/calendars/{cid}/import_image",
        json={"image_data_url": f"data:image/png;base64,{body}"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── /import_prompt — requires AI ───────────────────────────────────────


async def test_import_prompt_503_without_ai_adapter(client):
    cid = await _create_calendar(client)
    r = await client.post(
        f"/api/calendars/{cid}/import_prompt",
        json={"prompt": "dentist tomorrow 10am"},
        headers=_auth(client._tok),
    )
    assert r.status == 503


async def test_import_prompt_happy_path_with_ai(client):
    cid = await _create_calendar(client)
    inner = client.app[platform_adapter_key]
    _swap_import_service(client.app, _FakeAiAdapter(inner))
    r = await client.post(
        f"/api/calendars/{cid}/import_prompt",
        json={"prompt": "dentist tomorrow 10am"},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    assert len((await r.json())["events"]) == 1


async def test_import_prompt_missing_field_422(client):
    cid = await _create_calendar(client)
    inner = client.app[platform_adapter_key]
    _swap_import_service(client.app, _FakeAiAdapter(inner))
    r = await client.post(
        f"/api/calendars/{cid}/import_prompt",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_import_prompt_unauth_401(client):
    r = await client.post(
        "/api/calendars/X/import_prompt",
        json={"prompt": "hi"},
    )
    assert r.status == 401


# ─── all-day tz convention (bare ICS DATE is floating) ─────────────────


_ICS_ALL_DAY_AND_TIMED = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
    b"BEGIN:VEVENT\r\nUID:trip@t\r\nSUMMARY:Trip\r\n"
    b"DTSTART;VALUE=DATE:20260910\r\nDTEND;VALUE=DATE:20260913\r\n"
    b"END:VEVENT\r\n"
    b"BEGIN:VEVENT\r\nUID:call@t\r\nSUMMARY:Call\r\n"
    b"DTSTART:20260910T170000Z\r\nDTEND:20260910T180000Z\r\n"
    b"END:VEVENT\r\nEND:VCALENDAR\r\n"
)


async def test_import_ics_all_day_event_is_anchored_to_utc(client):
    """A bare-``DATE`` VEVENT stores UTC-midnight bounds, so its ``tz``
    must say ``UTC`` — otherwise a client reading the day in ``event.tz``
    (per docs/api.md) shifts it west of UTC.

    The timed VEVENT in the same import still resolves the household
    zone: the fix is scoped to all-day rows.
    """
    await client._db.enqueue(
        "UPDATE users SET tz=? WHERE username=?",
        ("America/Los_Angeles", "admin"),
    )

    cid = await _create_calendar(client)
    r = await client.post(
        f"/api/calendars/{cid}/import_ics",
        data=_ICS_ALL_DAY_AND_TIMED,
        headers={**_auth(client._tok), "Content-Type": "text/calendar"},
    )
    assert r.status == 201, await r.text()
    events = {e["summary"]: e for e in (await r.json())["events"]}
    assert set(events) == {"Trip", "Call"}

    trip = events["Trip"]
    assert trip["all_day"] is True
    assert trip["tz"] == "UTC"
    assert trip["start"] == "2026-09-10T00:00:00+00:00"
    # ICS DTEND on a bare DATE is exclusive — stored verbatim; the
    # client derives the last inclusive moment.
    assert trip["end"] == "2026-09-13T00:00:00+00:00"

    call = events["Call"]
    assert call["all_day"] is False
    assert call["tz"] == "America/Los_Angeles"

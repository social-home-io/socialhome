"""Integration tests for socialhome.routes.timetables (/api/timetables/*)."""

from __future__ import annotations

from datetime import timedelta

from socialhome.app_keys import event_bus_key
from socialhome.domain.events import TimetableSaved
from socialhome.domain.timetable import week_anchor
from socialhome.services import timetable_service as ts
from socialhome.services.timetable_service import MAX_TIMETABLES

from .conftest import _auth

#: Next week's Monday / Sunday from the service's own clock — overrides are
#: pruned 14 days after their date, so fixed dates would rot in CI. Tests
#: without overrides keep fixed dates.
MON = week_anchor(ts._utcnow().date() + timedelta(days=7), 0)
SUN = MON + timedelta(days=6)


def _h(client) -> dict:
    return _auth(client._tok)


async def _create(client, **body) -> dict:
    body.setdefault("name", "Anna 5b")
    r = await client.post("/api/timetables", json=body, headers=_h(client))
    assert r.status == 201, await r.text()
    return (await r.json())["timetable"]


async def _err(r, status: int) -> dict:
    assert r.status == status, await r.text()
    return (await r.json())["error"]


def _monday(tt: dict) -> list[dict]:
    return sorted(
        (e for e in tt["entries"] if e["weekday"] == 0), key=lambda e: e["start"]
    )


# ─── Collection ──────────────────────────────────────────────────────────


async def test_create_from_school_template(client):
    tt = await _create(client)
    assert tt["version"] == 1
    assert tt["schema"] == 1
    assert tt["days"] == [0, 1, 2, 3, 4]
    assert tt["assignees"] == [client._uid]
    assert tt["created_by"] == client._uid
    assert tt["tz"] == "UTC"  # household tz default
    assert isinstance(tt["active_this_week"], bool)
    assert isinstance(tt["valid_today"], bool)
    got = [(e["kind"], e["label"], e["start"], e["end"]) for e in _monday(tt)]
    assert got == [
        ("lesson", "1.", "08:00", "08:45"),
        ("lesson", "2.", "08:50", "09:35"),
        ("break", None, "09:35", "09:55"),
        ("lesson", "3.", "09:55", "10:40"),
        ("lesson", "4.", "10:45", "11:30"),
        ("break", None, "11:30", "11:45"),
        ("lesson", "5.", "11:45", "12:30"),
        ("lesson", "6.", "12:35", "13:20"),
    ]
    assert len(tt["entries"]) == 40


async def test_create_empty_with_options_and_list(client):
    await _create(client, name="Zeta")
    tt = await _create(
        client,
        name="Alpha",
        template="empty",
        week_start=6,
        days=[6, 0, 1, 2, 3],
        tz="Europe/Berlin",
        color="teal",
        assignees=[client._uid],
    )
    assert tt["entries"] == []
    assert tt["days"] == [0, 1, 2, 3, 6]
    assert tt["week_start"] == 6 and tt["tz"] == "Europe/Berlin"
    r = await client.get("/api/timetables", headers=_h(client))
    assert r.status == 200
    names = [t["name"] for t in (await r.json())["timetables"]]
    assert names == ["Alpha", "Zeta"]


async def test_create_validation_errors(client):
    for body in (
        {},
        {"name": "   "},
        {"name": 5},
        {"name": "x", "template": "nope"},
        {"name": "x", "color": "#ff0000"},
        {"name": "x", "tz": "Mars/Base"},
        {"name": "x", "days": []},
        {"name": "x", "days": [7]},
        {"name": "x", "week_start": 2},
        {"name": "x", "week_start": "0"},
        {"name": "x", "assignees": ["nobody"]},
        {"name": "x", "assignees": "me"},
    ):
        r = await client.post("/api/timetables", json=body, headers=_h(client))
        err = await _err(r, 422)
        assert err["code"] == "UNPROCESSABLE"


async def test_create_limit(client):
    for i in range(MAX_TIMETABLES):
        await _create(client, name=f"T{i}", template="empty")
    r = await client.post(
        "/api/timetables", json={"name": "one more"}, headers=_h(client)
    )
    assert (await _err(r, 409))["code"] == "TIMETABLE_LIMIT"


async def test_requires_auth(client):
    r = await client.get("/api/timetables")
    assert r.status == 401


async def test_feature_disabled_403(client):
    tt = await _create(client)
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_timetable": False}},
        headers=_h(client),
    )
    assert r.status == 200
    for method, path in (
        ("GET", "/api/timetables"),
        ("POST", "/api/timetables"),
        ("GET", "/api/timetables/day"),
        ("GET", f"/api/timetables/{tt['id']}"),
        ("DELETE", f"/api/timetables/{tt['id']}"),
        ("GET", f"/api/timetables/{tt['id']}/weeks/{MON.isoformat()}"),
    ):
        r = await client.request(method, path, json={"name": "x"}, headers=_h(client))
        err = await _err(r, 403)
        assert err["code"] == "FEATURE_DISABLED" and err["section"] == "timetable"


# ─── Detail ──────────────────────────────────────────────────────────────


async def test_get_patch_delete(client):
    tt = await _create(client)
    tid = tt["id"]
    r = await client.get(f"/api/timetables/{tid}", headers=_h(client))
    assert r.status == 200
    assert (await r.json())["timetable"]["id"] == tid

    r = await client.patch(
        f"/api/timetables/{tid}",
        json={
            "version": 1,
            "name": "Renamed",
            "color": "rose",
            "defaults": {"gap_minutes": 10},
        },
        headers=_h(client),
    )
    assert r.status == 200
    body = (await r.json())["timetable"]
    assert body["version"] == 2 and body["name"] == "Renamed"
    assert body["color"] == "rose"
    assert body["defaults"] == {
        "lesson_minutes": 45,
        "gap_minutes": 10,
        "day_start": "08:00",
    }
    assert body["updated_by"] == client._uid

    r = await client.delete(f"/api/timetables/{tid}", headers=_h(client))
    assert r.status == 200
    assert await r.json() == {"ok": True}
    r = await client.get(f"/api/timetables/{tid}", headers=_h(client))
    assert (await _err(r, 404))["code"] == "NOT_FOUND"


async def test_unknown_id_404(client):
    h = _h(client)
    for method, path, body in (
        ("GET", "/api/timetables/nope", None),
        ("PATCH", "/api/timetables/nope", {"version": 1, "name": "x"}),
        ("DELETE", "/api/timetables/nope", None),
        ("POST", "/api/timetables/nope/duplicate", {"name": "x"}),
        ("GET", f"/api/timetables/nope/weeks/{MON.isoformat()}", None),
        (
            "POST",
            "/api/timetables/nope/entries",
            {"version": 1, "weekday": 0, "start": "08:00", "end": "08:45"},
        ),
    ):
        r = await client.request(method, path, json=body, headers=h)
        assert r.status == 404, (method, path, await r.text())


async def test_stale_version_409(client):
    tt = await _create(client)
    tid = tt["id"]
    r = await client.patch(
        f"/api/timetables/{tid}", json={"version": 1, "name": "v2"}, headers=_h(client)
    )
    assert r.status == 200
    r = await client.patch(
        f"/api/timetables/{tid}",
        json={"version": 1, "name": "lost"},
        headers=_h(client),
    )
    err = await _err(r, 409)
    assert err["code"] == "TIMETABLE_CONFLICT"
    assert err["current_version"] == 2


async def test_version_required_and_typed(client):
    tt = await _create(client)
    tid = tt["id"]
    for body in ({"name": "x"}, {"version": "1", "name": "x"}, {"version": True}):
        r = await client.patch(f"/api/timetables/{tid}", json=body, headers=_h(client))
        await _err(r, 422)
    r = await client.delete(
        f"/api/timetables/{tid}/entries/{tt['entries'][0]['id']}?version=abc",
        headers=_h(client),
    )
    await _err(r, 422)


async def test_patch_days_orphans_then_drop(client):
    tt = await _create(client)
    tid = tt["id"]
    r = await client.patch(
        f"/api/timetables/{tid}",
        json={"version": 1, "days": [0, 1, 2, 3]},
        headers=_h(client),
    )
    err = await _err(r, 409)
    assert err["code"] == "DAYS_ORPHAN_ENTRIES"
    assert err["count"] == 8
    r = await client.patch(
        f"/api/timetables/{tid}",
        json={"version": 1, "days": [0, 1, 2, 3], "drop_orphans": True},
        headers=_h(client),
    )
    assert r.status == 200
    body = (await r.json())["timetable"]
    assert body["days"] == [0, 1, 2, 3]
    assert all(e["weekday"] != 4 for e in body["entries"])


async def test_patch_unknown_field_422(client):
    tt = await _create(client)
    r = await client.patch(
        f"/api/timetables/{tt['id']}",
        json={"version": 1, "bogus": 1},
        headers=_h(client),
    )
    await _err(r, 422)


async def test_duplicate(client):
    tt = await _create(client)
    r = await client.post(
        f"/api/timetables/{tt['id']}/duplicate",
        json={"name": "Ben 3a"},
        headers=_h(client),
    )
    assert r.status == 201
    dup = (await r.json())["timetable"]
    assert dup["id"] != tt["id"] and dup["name"] == "Ben 3a"
    assert dup["version"] == 1
    assert len(dup["entries"]) == len(tt["entries"])
    assert not {e["id"] for e in dup["entries"]} & {e["id"] for e in tt["entries"]}


# ─── Days ────────────────────────────────────────────────────────────────


async def test_generate_day_orphan_then_replace(client):
    tt = await _create(client)
    tid = tt["id"]
    slots = [
        {"start": "07:15", "end": "07:45", "title": "Musik"},
        {"start": "08:00", "end": "08:45"},
    ]
    r = await client.post(
        f"/api/timetables/{tid}/days/0/generate",
        json={"version": 1, "slots": slots},
        headers=_h(client),
    )
    err = await _err(r, 409)
    assert err["code"] == "DAY_HAS_ENTRIES" and err["count"] == 8
    r = await client.post(
        f"/api/timetables/{tid}/days/0/generate",
        json={"version": 1, "slots": slots, "replace": True},
        headers=_h(client),
    )
    assert r.status == 200
    monday = _monday((await r.json())["timetable"])
    assert [(e["start"], e["title"]) for e in monday] == [
        ("07:15", "Musik"),
        ("08:00", None),
    ]


async def test_bad_weekday_in_path_404(client):
    tt = await _create(client)
    r = await client.post(
        f"/api/timetables/{tt['id']}/days/7/generate",
        json={"version": 1, "slots": []},
        headers=_h(client),
    )
    # The ``{weekday:[0-6]}`` pattern refuses 7, so no timetable route
    # matches. What answers depends on whether a built SPA is present:
    # its GET catch-all turns a path miss into 405, without it aiohttp
    # returns 404 (CI's test job never builds the SPA). Either way the
    # handler never ran.
    assert r.status in (404, 405)


async def test_copy_and_shift_day(client):
    tt = await _create(client, template="empty")
    tid = tt["id"]
    r = await client.post(
        f"/api/timetables/{tid}/days/0/generate",
        json={
            "version": 1,
            "slots": [{"start": "08:00", "end": "08:45", "title": "Mathe"}],
        },
        headers=_h(client),
    )
    assert r.status == 200
    r = await client.post(
        f"/api/timetables/{tid}/days/0/copy",
        json={"version": 2, "to": [1, 2], "with_subjects": False},
        headers=_h(client),
    )
    assert r.status == 200
    body = (await r.json())["timetable"]
    assert sorted(e["weekday"] for e in body["entries"]) == [0, 1, 2]
    assert [e["title"] for e in body["entries"] if e["weekday"] == 1] == [None]
    r = await client.post(
        f"/api/timetables/{tid}/days/0/copy",
        json={"version": 3, "to": [1]},
        headers=_h(client),
    )
    assert (await _err(r, 409))["code"] == "DAY_HAS_ENTRIES"

    r = await client.post(
        f"/api/timetables/{tid}/days/1/shift",
        json={"version": 3, "from": "07:00", "minutes": 20},
        headers=_h(client),
    )
    assert r.status == 200
    body = (await r.json())["timetable"]
    (tue,) = [e for e in body["entries"] if e["weekday"] == 1]
    assert (tue["start"], tue["end"]) == ("08:20", "09:05")
    r = await client.post(
        f"/api/timetables/{tid}/days/1/shift",
        json={"version": 4, "from": "7am", "minutes": 20},
        headers=_h(client),
    )
    await _err(r, 422)


# ─── Entries ─────────────────────────────────────────────────────────────


async def test_entry_crud(client):
    tt = await _create(client, template="empty")
    tid = tt["id"]
    r = await client.post(
        f"/api/timetables/{tid}/entries",
        json={"version": 1, "weekday": 0, "start": "08:00", "end": "08:45"},
        headers=_h(client),
    )
    assert r.status == 200
    (entry,) = (await r.json())["timetable"]["entries"]
    eid = entry["id"]

    r = await client.patch(
        f"/api/timetables/{tid}/entries/{eid}",
        json={"version": 2, "title": "Mathe", "color": "sky"},
        headers=_h(client),
    )
    assert r.status == 200
    (entry,) = (await r.json())["timetable"]["entries"]
    assert entry["title"] == "Mathe" and entry["color"] == "sky"

    r = await client.patch(
        f"/api/timetables/{tid}/entries/nope",
        json={"version": 3, "title": "x"},
        headers=_h(client),
    )
    assert r.status == 404

    r = await client.put(
        f"/api/timetables/{tid}/entries",
        json={
            "version": 3,
            "entries": [
                {"id": eid, "weekday": 0, "start": "08:00", "end": "08:45"},
                {"weekday": 1, "start": "09:00", "end": "09:45"},
            ],
        },
        headers=_h(client),
    )
    assert r.status == 200
    entries = (await r.json())["timetable"]["entries"]
    assert len(entries) == 2 and eid in {e["id"] for e in entries}

    # DELETE: version in the query …
    r = await client.delete(
        f"/api/timetables/{tid}/entries/{eid}?version=4", headers=_h(client)
    )
    assert r.status == 200
    body = (await r.json())["timetable"]
    assert body["version"] == 5 and len(body["entries"]) == 1
    # … or in the body.
    other = body["entries"][0]["id"]
    r = await client.delete(
        f"/api/timetables/{tid}/entries/{other}",
        json={"version": 5},
        headers=_h(client),
    )
    assert r.status == 200
    assert (await r.json())["timetable"]["entries"] == []


async def test_entry_validation_422(client):
    tt = await _create(client)  # school template on Mon–Fri
    tid = tt["id"]
    for bad in (
        {"weekday": 0, "start": "08:30", "end": "09:00"},  # overlap
        {"weekday": 0, "start": "14:00", "end": "14:03"},  # < 5 min
        {"weekday": 5, "start": "08:00", "end": "08:45"},  # hidden Saturday
        {"weekday": 0, "start": "14:00", "end": "14:45", "color": "#abcdef"},
        {"weekday": 0, "start": "14:00", "end": "14:45", "kind": "nap"},
        {"weekday": 0, "start": "2pm", "end": "14:45"},
    ):
        r = await client.post(
            f"/api/timetables/{tid}/entries",
            json={"version": 1, **bad},
            headers=_h(client),
        )
        err = await _err(r, 422)
        assert err["code"] == "UNPROCESSABLE"
    r = await client.patch(
        f"/api/timetables/{tid}/entries/{tt['entries'][0]['id']}",
        json={"version": 1, "bogus": 1},
        headers=_h(client),
    )
    await _err(r, 422)


# ─── Validity + weeks ────────────────────────────────────────────────────


async def test_validity_and_sunday_week(client):
    tt = await _create(client, week_start=6, days=[6, 0, 1, 2, 3])
    tid = tt["id"]
    # Any day of the week snaps to the Sunday anchor.
    r = await client.put(
        f"/api/timetables/{tid}/validity",
        json={
            "version": 1,
            "valid_from": "2026-09-01",
            "valid_until": "2027-07-31",
            "excluded_weeks": ["2026-10-14"],
        },
        headers=_h(client),
    )
    assert r.status == 200
    body = (await r.json())["timetable"]
    assert body["validity"] == {
        "valid_from": "2026-09-01",
        "valid_until": "2027-07-31",
        "excluded_weeks": ["2026-10-11"],
    }

    # Saturday 2026-10-03 belongs to the week starting Sunday 2026-09-27.
    r = await client.get(f"/api/timetables/{tid}/weeks/2026-10-03", headers=_h(client))
    assert r.status == 200
    week = (await r.json())["week"]
    assert week["anchor"] == "2026-09-27"
    assert week["valid"] is True
    assert [d["date"] for d in week["days"]] == [
        "2026-09-27",
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-01",
    ]
    assert len(week["days"][0]["lessons"]) == 8

    r = await client.get(f"/api/timetables/{tid}/weeks/2026-10-12", headers=_h(client))
    week = (await r.json())["week"]
    assert week["anchor"] == "2026-10-11" and week["valid"] is False

    r = await client.get(f"/api/timetables/{tid}/weeks/garbage", headers=_h(client))
    await _err(r, 422)

    r = await client.put(
        f"/api/timetables/{tid}/validity",
        json={"version": 2, "valid_from": "2027-01-01", "valid_until": "2026-01-01"},
        headers=_h(client),
    )
    await _err(r, 422)


# ─── Overrides ───────────────────────────────────────────────────────────


async def test_override_crud_and_clear_week(client):
    tt = await _create(client)
    tid = tt["id"]
    first = _monday(tt)[0]
    r = await client.post(
        f"/api/timetables/{tid}/overrides",
        json={
            "version": 1,
            "date": MON.isoformat(),
            "kind": "replace",
            "entry_id": first["id"],
            "room": "Aula",
        },
        headers=_h(client),
    )
    assert r.status == 200
    (ov,) = (await r.json())["timetable"]["overrides"]

    r = await client.patch(
        f"/api/timetables/{tid}/overrides/{ov['id']}",
        json={"version": 2, "kind": "cancel", "room": None},
        headers=_h(client),
    )
    assert r.status == 200
    r = await client.get(
        f"/api/timetables/{tid}/weeks/{MON.isoformat()}", headers=_h(client)
    )
    lessons = (await r.json())["week"]["days"][0]["lessons"]
    assert lessons[0]["status"] == "cancelled"
    assert lessons[0]["original"]["id"] == first["id"]

    r = await client.post(
        f"/api/timetables/{tid}/overrides",
        json={
            "version": 3,
            "date": (MON + timedelta(days=1)).isoformat(),
            "kind": "add",
            "start": "14:00",
            "end": "15:00",
            "title": "AG",
        },
        headers=_h(client),
    )
    assert r.status == 200
    r = await client.post(
        f"/api/timetables/{tid}/overrides",
        json={
            "version": 4,
            "date": (MON + timedelta(days=7)).isoformat(),
            "kind": "add",
            "start": "14:00",
            "end": "15:00",
        },
        headers=_h(client),
    )
    assert r.status == 200
    assert len((await r.json())["timetable"]["overrides"]) == 3

    r = await client.delete(
        f"/api/timetables/{tid}/weeks/{(MON + timedelta(days=3)).isoformat()}"
        "/overrides?version=5",
        headers=_h(client),
    )
    assert r.status == 200
    remaining = (await r.json())["timetable"]["overrides"]
    assert [o["date"] for o in remaining] == [(MON + timedelta(days=7)).isoformat()]

    r = await client.delete(
        f"/api/timetables/{tid}/overrides/{remaining[0]['id']}?version=6",
        headers=_h(client),
    )
    assert r.status == 200
    assert (await r.json())["timetable"]["overrides"] == []

    r = await client.delete(
        f"/api/timetables/{tid}/overrides/nope?version=7", headers=_h(client)
    )
    assert r.status == 404


async def test_override_validation_422(client):
    tt = await _create(client)
    tid = tt["id"]
    for bad in (
        {
            "date": (MON + timedelta(days=5)).isoformat(),  # hidden Saturday
            "kind": "add",
            "start": "10:00",
            "end": "11:00",
        },
        {"date": MON.isoformat(), "kind": "cancel", "entry_id": "nope"},
        {"date": MON.isoformat(), "kind": "add", "start": "08:00", "end": "08:30"},
        {"date": "not-a-date", "kind": "add", "start": "10:00", "end": "11:00"},
    ):
        r = await client.post(
            f"/api/timetables/{tid}/overrides",
            json={"version": 1, **bad},
            headers=_h(client),
        )
        await _err(r, 422)


# ─── /day ────────────────────────────────────────────────────────────────


async def _add_user(client, username: str, uid: str) -> None:
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, uid, username.title()),
    )


async def test_day_lists_only_my_valid_timetables(client):
    await _add_user(client, "ben", "u-ben")
    mine = await _create(client, name="Mine")
    await _create(client, name="Ben's", assignees=["u-ben"])
    held = await _create(client, name="Held")
    r = await client.put(
        f"/api/timetables/{held['id']}/validity",
        json={"version": 1, "excluded_weeks": [MON.isoformat()]},
        headers=_h(client),
    )
    assert r.status == 200

    r = await client.get(
        f"/api/timetables/day?date={MON.isoformat()}", headers=_h(client)
    )
    assert r.status == 200
    body = await r.json()
    assert body["date"] == MON.isoformat()
    assert [t["timetable_id"] for t in body["timetables"]] == [mine["id"]]
    (entry,) = body["timetables"]
    assert entry["name"] == "Mine" and entry["color"] is None
    assert [ls["start"] for ls in entry["lessons"]][:2] == ["08:00", "08:50"]
    assert entry["lessons"][0]["status"] == "normal"

    # Sunday: not one of the days → nothing.
    r = await client.get(
        f"/api/timetables/day?date={SUN.isoformat()}", headers=_h(client)
    )
    assert (await r.json())["timetables"] == []


async def test_day_defaults_to_today_and_rejects_bad_date(client):
    r = await client.get("/api/timetables/day", headers=_h(client))
    assert r.status == 200
    assert (await r.json())["date"] == ts._utcnow().date().isoformat()
    r = await client.get("/api/timetables/day?date=31.12.2026", headers=_h(client))
    await _err(r, 422)


async def test_realtime_event_published_on_mutation(client):
    """A create lands on the bus as TimetableSaved (→ ``timetable.changed``)."""
    seen = []

    async def grab(event):
        seen.append(event)

    client.app[event_bus_key].subscribe(TimetableSaved, grab)
    tt = await _create(client)
    assert [e.timetable.id for e in seen] == [tt["id"]]


async def test_patch_entry_icon(client):
    tt = await _create(client)
    tid, eid = tt["id"], _monday(tt)[0]["id"]
    r = await client.patch(
        f"/api/timetables/{tid}/entries/{eid}",
        json={"version": 1, "icon": "⚽"},
        headers=_h(client),
    )
    assert r.status == 200
    entry = next(e for e in (await r.json())["timetable"]["entries"] if e["id"] == eid)
    assert entry["icon"] == "⚽"
    r = await client.patch(
        f"/api/timetables/{tid}/entries/{eid}",
        json={"version": 2, "icon": "<b>"},
        headers=_h(client),
    )
    await _err(r, 422)


async def test_request_shape_guards_422(client):
    """Wrong JSON types are 422s, never 500s."""
    tt = await _create(client)
    tid = tt["id"]
    h = _h(client)
    cases = [
        ("POST", "/api/timetables", ["not", "an", "object"]),
        ("POST", "/api/timetables", {"name": "x", "color": 5}),
        ("POST", "/api/timetables", {"name": "x", "tz": 5}),
        ("POST", "/api/timetables", {"name": "x", "template": ["school"]}),
        ("PATCH", f"/api/timetables/{tid}", {"version": 0, "name": "x"}),
        ("PATCH", f"/api/timetables/{tid}", {"version": 1, "color": 5}),
        ("PATCH", f"/api/timetables/{tid}", {"version": 1, "tz": 5}),
        ("PATCH", f"/api/timetables/{tid}", {"version": 1, "week_start": "6"}),
        ("PATCH", f"/api/timetables/{tid}", {"version": 1, "days": "0,1"}),
        ("PATCH", f"/api/timetables/{tid}", {"version": 1, "assignees": [5]}),
        ("PATCH", f"/api/timetables/{tid}", {"version": 1, "drop_orphans": "yes"}),
        (
            "POST",
            f"/api/timetables/{tid}/days/0/generate",
            {"version": 1, "slots": [], "replace": 1},
        ),
    ]
    for method, path, body in cases:
        r = await client.request(method, path, json=body, headers=h)
        assert r.status == 422, (method, path, body, await r.text())


async def test_patch_week_start_remaps_excluded_weeks(client):
    tt = await _create(client, template="empty")
    tid = tt["id"]
    r = await client.put(
        f"/api/timetables/{tid}/validity",
        json={"version": 1, "excluded_weeks": [MON.isoformat()]},
        headers=_h(client),
    )
    assert r.status == 200
    r = await client.patch(
        f"/api/timetables/{tid}",
        json={"version": 2, "week_start": 6, "days": [0, 1, 2, 3, 4, 6]},
        headers=_h(client),
    )
    assert r.status == 200
    body = (await r.json())["timetable"]
    assert body["week_start"] == 6
    assert body["validity"]["excluded_weeks"] == [(MON - timedelta(days=1)).isoformat()]
    r = await client.patch(
        f"/api/timetables/{tid}",
        json={"version": 3, "assignees": []},
        headers=_h(client),
    )
    assert (await r.json())["timetable"]["assignees"] == []


# ─── Review hardening ────────────────────────────────────────────────────


async def test_out_of_range_dates_are_422_not_500(client):
    h = _h(client)
    tt = await _create(client)
    sun = await _create(client, name="Sun", week_start=6, days=[6, 0, 1, 2, 3])
    tid, sid = tt["id"], sun["id"]
    for method, path, body in (
        ("GET", f"/api/timetables/{tid}/weeks/9999-12-31", None),
        ("GET", f"/api/timetables/{sid}/weeks/0001-01-01", None),
        ("GET", "/api/timetables/day?date=0001-01-01", None),
        ("DELETE", f"/api/timetables/{tid}/weeks/9999-12-31/overrides?version=1", None),
        (
            "PUT",
            f"/api/timetables/{tid}/validity",
            {"version": 1, "excluded_weeks": ["0001-01-01"]},
        ),
        (
            "POST",
            f"/api/timetables/{tid}/overrides",
            {
                "version": 1,
                "date": "9999-12-27",
                "kind": "add",
                "start": "15:00",
                "end": "16:00",
            },
        ),
    ):
        r = await client.request(method, path, json=body, headers=h)
        assert r.status == 422, (method, path, await r.text())
    # An edge week that is valid Monday-start but not after a week_start flip.
    r = await client.put(
        f"/api/timetables/{tid}/validity",
        json={"version": 1, "excluded_weeks": ["1900-01-01"]},
        headers=h,
    )
    assert r.status == 200
    r = await client.patch(
        f"/api/timetables/{tid}",
        json={"version": 2, "week_start": 6, "days": [0, 1, 2, 3, 4, 6]},
        headers=h,
    )
    await _err(r, 422)


async def test_lone_surrogates_are_422_not_500(client):
    tt = await _create(client)
    tid = tt["id"]
    r = await client.post(
        f"/api/timetables/{tid}/entries",
        json={
            "version": 1,
            "weekday": 0,
            "start": "14:00",
            "end": "14:45",
            "title": "\ud800",
        },
        headers=_h(client),
    )
    assert "UTF-8" in (await _err(r, 422))["detail"]
    r = await client.patch(
        f"/api/timetables/{tid}",
        json={"version": 1, "name": "A\udfff"},
        headers=_h(client),
    )
    assert "UTF-8" in (await _err(r, 422))["detail"]
    r = await client.post(
        "/api/timetables", json={"name": "\ud800"}, headers=_h(client)
    )
    await _err(r, 422)


async def test_unknown_keys_on_add_and_generate_422(client):
    tt = await _create(client)
    tid = tt["id"]
    first = _monday(tt)[0]["id"]
    for path, body in (
        (
            f"/api/timetables/{tid}/entries",
            {"version": 1, "weekday": 0, "start": "14:00", "end": "14:45", "bogus": 1},
        ),
        (
            f"/api/timetables/{tid}/entries",
            {
                "version": 1,
                "id": "mine",
                "weekday": 0,
                "start": "14:00",
                "end": "14:45",
            },
        ),
        (
            f"/api/timetables/{tid}/overrides",
            {
                "version": 1,
                "date": MON.isoformat(),
                "kind": "cancel",
                "entry_id": first,
                "x": 1,
            },
        ),
        (
            f"/api/timetables/{tid}/days/0/generate",
            {
                "version": 1,
                "replace": True,
                "slots": [{"start": "08:00", "end": "08:45", "x": 1}],
            },
        ),
    ):
        r = await client.post(path, json=body, headers=_h(client))
        assert "unknown field" in (await _err(r, 422))["detail"]
    r = await client.put(
        f"/api/timetables/{tid}/entries",
        json={
            "version": 1,
            "entries": [{"weekday": 0, "start": "08:00", "end": "08:45", "x": 1}],
        },
        headers=_h(client),
    )
    await _err(r, 422)


async def test_put_entries_mints_unknown_client_ids(client):
    tt = await _create(client, template="empty")
    r = await client.put(
        f"/api/timetables/{tt['id']}/entries",
        json={
            "version": 1,
            "entries": [
                {"id": "client-chosen", "weekday": 0, "start": "08:00", "end": "08:45"}
            ],
        },
        headers=_h(client),
    )
    assert r.status == 200
    (entry,) = (await r.json())["timetable"]["entries"]
    assert entry["id"] != "client-chosen"


async def test_delete_version_must_be_ascii_digits(client):
    tt = await _create(client)
    eid = tt["entries"][0]["id"]
    for raw in ("\u0661", "\u00b2", "1e3", "-1", "0", "12345678901"):
        r = await client.delete(
            f"/api/timetables/{tt['id']}/entries/{eid}",
            params={"version": raw},
            headers=_h(client),
        )
        await _err(r, 422)


async def test_duplicate_without_body(client):
    tt = await _create(client)
    r = await client.post(f"/api/timetables/{tt['id']}/duplicate", headers=_h(client))
    assert r.status == 201, await r.text()
    assert (await r.json())["timetable"]["name"] == "Anna 5b (copy)"


async def test_old_override_is_422(client):
    tt = await _create(client)
    old = ts._utcnow().date() - timedelta(days=20)
    while old.weekday() > 4:
        old -= timedelta(days=1)
    r = await client.post(
        f"/api/timetables/{tt['id']}/overrides",
        json={
            "version": 1,
            "date": old.isoformat(),
            "kind": "add",
            "start": "15:00",
            "end": "16:00",
        },
        headers=_h(client),
    )
    assert "past" in (await _err(r, 422))["detail"]


async def test_patch_assignees_null_is_422(client):
    tt = await _create(client)
    r = await client.patch(
        f"/api/timetables/{tt['id']}",
        json={"version": 1, "assignees": None},
        headers=_h(client),
    )
    await _err(r, 422)


async def test_bad_ids_in_body_422(client):
    tt = await _create(client, template="empty")
    r = await client.post(
        "/api/timetables",
        json={"name": "x", "assignees": ["has space"]},
        headers=_h(client),
    )
    await _err(r, 422)
    r = await client.post(
        f"/api/timetables/{tt['id']}/overrides",
        json={
            "version": 1,
            "date": MON.isoformat(),
            "kind": "cancel",
            "entry_id": "../x",
        },
        headers=_h(client),
    )
    await _err(r, 422)

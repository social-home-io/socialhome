"""Integration tests for ``/api/spaces/{space_id}/timetables/*``.

A space's shared timetables: members (followers included) read, owners /
admins edit, everything behind the space's ``timetable`` feature.
"""

from __future__ import annotations

from datetime import timedelta

from socialhome.auth import sha256_token_hash
from socialhome.domain.timetable import week_anchor
from socialhome.services import timetable_service as ts
from socialhome.services.timetable_service import MAX_SPACE_TIMETABLES

from .conftest import _auth

MON = week_anchor(ts._utcnow().date() + timedelta(days=7), 0)


async def _add_user(client, name: str, *, space_id: str | None, role: str) -> dict:
    uid = f"{name}-id"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES(?, ?, ?, 0)",
        (name, uid, name.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES(?, ?, 't', ?)",
        (f"t-{name}", uid, sha256_token_hash(f"{name}-tok")),
    )
    if space_id is not None:
        await client._db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (space_id, uid, role),
        )
    return _auth(f"{name}-tok")


async def _space(client, *, name: str = "Klasse 5b", timetable: bool = True) -> str:
    r = await client.post(
        "/api/spaces",
        json={"name": name, "space_type": "household", "join_mode": "open"},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    await client._db.enqueue(
        "UPDATE spaces SET feature_timetable=? WHERE id=?", (int(timetable), sid)
    )
    return sid


def _base(sid: str) -> str:
    return f"/api/spaces/{sid}/timetables"


async def _create(client, sid: str, **body) -> dict:
    body.setdefault("name", "Stundenplan")
    r = await client.post(_base(sid), json=body, headers=_auth(client._tok))
    assert r.status == 201, await r.text()
    return (await r.json())["timetable"]


async def _err(r, status: int) -> dict:
    assert r.status == status, await r.text()
    return (await r.json())["error"]


async def test_owner_creates_and_a_member_reads(client):
    sid = await _space(client)
    tt = await _create(client, sid)
    assert tt["assignees"] == [] and tt["created_by"] == client._uid
    assert len(tt["entries"]) == 40  # school template
    member = await _add_user(client, "bob", space_id=sid, role="member")
    r = await client.get(_base(sid), headers=member)
    assert r.status == 200
    assert [t["id"] for t in (await r.json())["timetables"]] == [tt["id"]]
    r = await client.get(f"{_base(sid)}/{tt['id']}", headers=member)
    assert r.status == 200 and (await r.json())["timetable"]["id"] == tt["id"]
    r = await client.get(
        f"{_base(sid)}/{tt['id']}/weeks/{MON.isoformat()}", headers=member
    )
    assert r.status == 200 and len((await r.json())["week"]["days"]) == 5


async def test_a_follower_reads_but_cannot_edit(client):
    sid = await _space(client)
    tt = await _create(client, sid)
    sub = await _add_user(client, "sue", space_id=sid, role="subscriber")
    assert (await client.get(_base(sid), headers=sub)).status == 200
    r = await client.patch(
        f"{_base(sid)}/{tt['id']}", json={"version": 1, "name": "x"}, headers=sub
    )
    await _err(r, 403)


async def test_non_member_is_forbidden(client):
    sid = await _space(client)
    tt = await _create(client, sid)
    stranger = await _add_user(client, "eve", space_id=None, role="member")
    for method, url, body in (
        ("get", _base(sid), None),
        ("get", f"{_base(sid)}/{tt['id']}", None),
        ("post", _base(sid), {"name": "mine"}),
        ("delete", f"{_base(sid)}/{tt['id']}", None),
    ):
        r = await getattr(client, method)(url, json=body, headers=stranger)
        await _err(r, 403)


async def test_member_writes_are_forbidden_admin_writes_land(client):
    sid = await _space(client)
    tt = await _create(client, sid, template="empty")
    member = await _add_user(client, "bob", space_id=sid, role="member")
    admin = await _add_user(client, "ada", space_id=sid, role="admin")
    tid = tt["id"]
    entry = {"version": 1, "weekday": 0, "start": "08:00", "end": "08:45"}
    for method, url, body in (
        ("post", _base(sid), {"name": "mine"}),
        ("patch", f"{_base(sid)}/{tid}", {"version": 1, "name": "x"}),
        ("delete", f"{_base(sid)}/{tid}", None),
        ("post", f"{_base(sid)}/{tid}/duplicate", {}),
        ("post", f"{_base(sid)}/{tid}/entries", entry),
        ("put", f"{_base(sid)}/{tid}/entries", {"version": 1, "entries": []}),
        ("post", f"{_base(sid)}/{tid}/days/0/generate", {"version": 1, "slots": []}),
        ("post", f"{_base(sid)}/{tid}/days/0/copy", {"version": 1, "to": [1]}),
        (
            "post",
            f"{_base(sid)}/{tid}/days/0/shift",
            {"version": 1, "from": "08:00", "minutes": 5},
        ),
        ("put", f"{_base(sid)}/{tid}/validity", {"version": 1}),
        (
            "delete",
            f"{_base(sid)}/{tid}/weeks/{MON.isoformat()}/overrides?version=1",
            None,
        ),
    ):
        r = await getattr(client, method)(url, json=body, headers=member)
        await _err(r, 403)
    r = await client.post(f"{_base(sid)}/{tid}/entries", json=entry, headers=admin)
    assert r.status == 200, await r.text()
    body = (await r.json())["timetable"]
    assert body["version"] == 2 and body["updated_by"] == "ada-id"


async def test_feature_off_is_forbidden(client):
    sid = await _space(client, timetable=False)
    r = await client.get(_base(sid), headers=_auth(client._tok))
    assert (await _err(r, 403))["code"] == "FEATURE_DISABLED"
    r = await client.post(_base(sid), json={"name": "x"}, headers=_auth(client._tok))
    assert (await _err(r, 403))["code"] == "FEATURE_DISABLED"


async def test_cross_space_id_is_not_found(client):
    a = await _space(client, name="A")
    b = await _space(client, name="B")
    tt = await _create(client, a)
    h = _auth(client._tok)
    await _err(await client.get(f"{_base(b)}/{tt['id']}", headers=h), 404)
    r = await client.patch(
        f"{_base(b)}/{tt['id']}", json={"version": 1, "name": "moved"}, headers=h
    )
    await _err(r, 404)
    await _err(await client.delete(f"{_base(b)}/{tt['id']}", headers=h), 404)
    r = await client.get(f"{_base(a)}/{tt['id']}", headers=h)
    assert (await r.json())["timetable"]["name"] == "Stundenplan"


async def test_the_per_space_limit(client):
    sid = await _space(client)
    for i in range(MAX_SPACE_TIMETABLES):
        await _create(client, sid, name=f"T{i}", template="empty")
    r = await client.post(
        _base(sid), json={"name": "one more"}, headers=_auth(client._tok)
    )
    assert (await _err(r, 409))["code"] == "TIMETABLE_LIMIT"


async def test_assignees_are_refused(client):
    sid = await _space(client)
    r = await client.post(
        _base(sid),
        json={"name": "x", "assignees": [client._uid]},
        headers=_auth(client._tok),
    )
    await _err(r, 422)


async def test_delete_leaves_a_tombstone(client):
    sid = await _space(client)
    tt = await _create(client, sid)
    h = _auth(client._tok)
    r = await client.delete(f"{_base(sid)}/{tt['id']}", headers=h)
    assert r.status == 200 and (await r.json()) == {"ok": True}
    row = await client._db.fetchone(
        "SELECT deleted_at, entries_json FROM space_timetables WHERE id=?",
        (tt["id"],),
    )
    assert row["deleted_at"] is not None and row["entries_json"] == "[]"
    await _err(await client.get(f"{_base(sid)}/{tt['id']}", headers=h), 404)
    assert (await (await client.get(_base(sid), headers=h)).json()) == {
        "timetables": []
    }


async def test_full_crud(client):
    sid = await _space(client)
    h = _auth(client._tok)
    tt = await _create(client, sid, template="empty", tz="Europe/Berlin")
    url = f"{_base(sid)}/{tt['id']}"
    v = 1

    async def ok(method: str, path: str, body=None, status: int = 200) -> dict:
        nonlocal v
        r = await getattr(client, method)(path, json=body, headers=h)
        assert r.status == status, await r.text()
        out = (await r.json())["timetable"]
        v = out["version"]
        return out

    got = await ok("patch", url, {"version": v, "name": "Renamed", "color": "teal"})
    assert (got["name"], got["color"]) == ("Renamed", "teal")
    got = await ok(
        "post",
        f"{url}/entries",
        {"version": v, "weekday": 0, "start": "08:00", "end": "08:45", "title": "Ma"},
    )
    eid = got["entries"][0]["id"]
    got = await ok("patch", f"{url}/entries/{eid}", {"version": v, "room": "B2"})
    assert got["entries"][0]["room"] == "B2"
    got = await ok(
        "post",
        f"{url}/days/1/generate",
        {"version": v, "slots": [{"start": "09:00", "end": "09:45"}]},
    )
    assert len(got["entries"]) == 2
    got = await ok("post", f"{url}/days/0/copy", {"version": v, "to": [2]})
    assert {e["weekday"] for e in got["entries"]} == {0, 1, 2}
    got = await ok(
        "post", f"{url}/days/0/shift", {"version": v, "from": "08:00", "minutes": 5}
    )
    got = await ok(
        "put",
        f"{url}/validity",
        {"version": v, "valid_from": None, "valid_until": None, "excluded_weeks": []},
    )
    got = await ok(
        "post",
        f"{url}/overrides",
        {
            "version": v,
            "date": MON.isoformat(),
            "kind": "replace",
            "entry_id": eid,
            "room": "C1",
        },
    )
    oid = got["overrides"][0]["id"]
    got = await ok("patch", f"{url}/overrides/{oid}", {"version": v, "room": "C3"})
    assert got["overrides"][0]["room"] == "C3"
    got = await ok("delete", f"{url}/overrides/{oid}?version={v}")
    assert got["overrides"] == []
    await ok(
        "post",
        f"{url}/overrides",
        {"version": v, "date": MON.isoformat(), "kind": "cancel", "entry_id": eid},
    )
    got = await ok("delete", f"{url}/weeks/{MON.isoformat()}/overrides?version={v}")
    assert got["overrides"] == []
    got = await ok("delete", f"{url}/entries/{eid}?version={v}")
    got = await ok("put", f"{url}/entries", {"version": v, "entries": []})
    assert got["entries"] == []
    dup = await ok("post", f"{url}/duplicate", {"name": "Copy"}, status=201)
    assert dup["id"] != tt["id"] and dup["name"] == "Copy"
    r = await client.patch(url, json={"version": 1, "name": "stale"}, headers=h)
    assert (await _err(r, 409))["code"] == "TIMETABLE_CONFLICT"


async def test_a_pinned_space_timetable_shows_on_the_home_today_card(client):
    """``preferences.timetable_home_pins`` adds a space timetable to the
    Today card of ``/api/me/corner`` — until its member leaves the space."""
    sid = await _space(client)
    member = await _add_user(client, "bob", space_id=sid, role="member")
    tt = await _create(
        client, sid, template="empty", days=[0, 1, 2, 3, 4, 5, 6], tz="UTC"
    )
    today = ts._utcnow().date()
    r = await client.post(
        f"{_base(sid)}/{tt['id']}/entries",
        json={
            "version": 1,
            "weekday": today.weekday(),
            "start": "08:00",
            "end": "08:45",
            "title": "Mathe",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 200, await r.text()

    async def today_ids() -> list[str]:
        r = await client.get("/api/me/corner", headers=member)
        assert r.status == 200, await r.text()
        return [t["timetable_id"] for t in (await r.json())["today_timetable"]]

    assert await today_ids() == []
    r = await client.patch(
        "/api/me",
        json={"preferences": {"timetable_home_pins": [tt["id"]]}},
        headers=member,
    )
    assert r.status == 200, await r.text()
    assert await today_ids() == [tt["id"]]
    await client._db.enqueue(
        "DELETE FROM space_members WHERE space_id=? AND user_id='bob-id'", (sid,)
    )
    assert await today_ids() == []

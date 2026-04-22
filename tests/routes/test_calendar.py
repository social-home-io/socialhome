"""Tests for socialhome.routes.calendar."""

from datetime import datetime, timezone, timedelta
from .conftest import _auth


async def test_create_calendar(client):
    """POST /api/calendars creates a calendar."""
    r = await client.post(
        "/api/calendars", json={"name": "Work"}, headers=_auth(client._tok)
    )
    assert r.status == 201


async def test_list_calendars(client):
    """GET /api/calendars returns user's calendars."""
    await client.post(
        "/api/calendars", json={"name": "Personal"}, headers=_auth(client._tok)
    )
    r = await client.get("/api/calendars", headers=_auth(client._tok))
    assert r.status == 200
    assert len(await r.json()) >= 1


async def test_create_event(client):
    """POST /api/calendars/{id}/events creates an event."""
    r = await client.post(
        "/api/calendars", json={"name": "C"}, headers=_auth(client._tok)
    )
    cid = (await r.json())["id"]
    now = datetime.now(timezone.utc)
    r2 = await client.post(
        f"/api/calendars/{cid}/events",
        json={
            "summary": "Meeting",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    assert r2.status == 201


# ─── Space-scoped calendar + RSVP (§23.7) ─────────────────────────────────


async def _seed_space(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES('sp-cal', 'Cal', 'iid', 'admin', ?, 'household')",
        ("aa" * 32,),
    )
    # Membership is required to create / RSVP / edit — the test user
    # is already inserted as 'admin' by the client fixture, so add
    # them to the space as a member.
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role)"
        " VALUES('sp-cal', ?, 'admin')",
        (client._uid,),
    )


async def test_space_create_and_list_events(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Stand-up",
            "start": now.isoformat(),
            "end": (now + timedelta(minutes=30)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    eid = (await r.json())["id"]

    # Use Z-suffix so the `+` of +00:00 doesn't get URL-decoded to a space.
    start_q = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
    end_q = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
    r2 = await client.get(
        f"/api/spaces/sp-cal/calendar/events?start={start_q}&end={end_q}",
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    events = await r2.json()
    assert any(e["id"] == eid for e in events)


async def test_space_create_event_requires_start_end(client):
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={"summary": "No times"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_rsvp_roundtrip_and_broadcast(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Party",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]

    r2 = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    body = await r2.json()
    assert body["counts"]["going"] == 1

    r3 = await client.get(
        f"/api/calendars/events/{eid}/rsvps",
        headers=_auth(client._tok),
    )
    assert r3.status == 200
    rsvps = (await r3.json())["rsvps"]
    assert any(r["status"] == "going" for r in rsvps)


async def test_rsvp_invalid_status_422(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "X",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "maybe-later"},
        headers=_auth(client._tok),
    )
    assert r2.status == 422


async def test_space_delete_event(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Gone",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    r2 = await client.delete(
        f"/api/spaces/sp-cal/calendar/events/{eid}",
        headers=_auth(client._tok),
    )
    assert r2.status == 200


# ─── Non-member gating + PATCH route ────────────────────────────────────


async def _seed_outsider(client):
    """Register a second user with no space membership and return auth."""
    from socialhome.auth import sha256_token_hash

    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('outsider', 'out-id', 'Out', 0)",
    )
    raw = "out-tok"
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('to1', 'out-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    return {"Authorization": f"Bearer {raw}"}


async def test_space_create_event_non_member_403(client):
    await _seed_space(client)
    outsider = await _seed_outsider(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Blocked",
            "start": now.isoformat(),
            "end": (now + timedelta(minutes=15)).isoformat(),
        },
        headers=outsider,
    )
    assert r.status == 403


async def test_rsvp_non_member_403(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Party",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    outsider = await _seed_outsider(client)
    r2 = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=outsider,
    )
    assert r2.status == 403


async def test_space_event_patch_updates_fields(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Old title",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]

    r2 = await client.patch(
        f"/api/spaces/sp-cal/calendar/events/{eid}",
        json={"summary": "New title", "description": "hello"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    body = await r2.json()
    assert body["summary"] == "New title"
    assert body["description"] == "hello"


async def test_space_event_patch_non_member_403(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "X",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    outsider = await _seed_outsider(client)
    r2 = await client.patch(
        f"/api/spaces/sp-cal/calendar/events/{eid}",
        json={"summary": "Y"},
        headers=outsider,
    )
    assert r2.status == 403


async def test_space_event_patch_missing_404(client):
    await _seed_space(client)
    r = await client.patch(
        "/api/spaces/sp-cal/calendar/events/nope",
        json={"summary": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 404

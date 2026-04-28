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


# ─── DELETE RSVP + occurrence_at (Phase A) ─────────────────────────────────


async def test_rsvp_delete_clears_response(client):
    """DELETE /api/calendars/events/{id}/rsvp removes the row."""
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Dinner",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=_auth(client._tok),
    )
    # Confirm the row is there.
    r2 = await client.get(
        f"/api/calendars/events/{eid}/rsvps",
        headers=_auth(client._tok),
    )
    assert len((await r2.json())["rsvps"]) == 1
    # DELETE clears it.
    r3 = await client.delete(
        f"/api/calendars/events/{eid}/rsvp",
        headers=_auth(client._tok),
    )
    assert r3.status == 200
    body = await r3.json()
    assert body["counts"]["going"] == 0
    r4 = await client.get(
        f"/api/calendars/events/{eid}/rsvps",
        headers=_auth(client._tok),
    )
    assert (await r4.json())["rsvps"] == []


async def test_rsvp_delete_non_member_403(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Anniversary",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    outsider = await _seed_outsider(client)
    r2 = await client.delete(
        f"/api/calendars/events/{eid}/rsvp",
        headers=outsider,
    )
    assert r2.status == 403


async def test_rsvp_recurring_per_occurrence(client):
    """Two POSTs with different occurrence_at values create two rows."""
    await _seed_space(client)
    seed = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Weekly meet",
            "start": seed.isoformat(),
            "end": (seed + timedelta(minutes=30)).isoformat(),
            "rrule": "FREQ=WEEKLY;COUNT=4",
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    occ1 = seed.isoformat()
    occ2 = (seed + timedelta(weeks=1)).isoformat()
    r1 = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going", "occurrence_at": occ1},
        headers=_auth(client._tok),
    )
    assert r1.status == 200
    r2 = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "declined", "occurrence_at": occ2},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    # Per-occurrence count: only 1 going on occ1
    body1 = await r1.json()
    assert body1["counts"]["going"] == 1
    # Listing across all occurrences returns both
    listing = await client.get(
        f"/api/calendars/events/{eid}/rsvps",
        headers=_auth(client._tok),
    )
    rsvps = (await listing.json())["rsvps"]
    assert len(rsvps) == 2
    occs = {r["occurrence_at"] for r in rsvps}
    assert occs == {occ1, occ2}
    # Listing scoped to occurrence_at returns just one. URL-encode the
    # `+` in the timezone offset so it survives the query parser.
    from urllib.parse import quote

    listing2 = await client.get(
        f"/api/calendars/events/{eid}/rsvps?occurrence_at={quote(occ1)}",
        headers=_auth(client._tok),
    )
    assert len((await listing2.json())["rsvps"]) == 1


async def test_rsvp_recurring_without_occurrence_422(client):
    """Recurring event RSVP without occurrence_at → 422."""
    await _seed_space(client)
    seed = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Weekly meet",
            "start": seed.isoformat(),
            "end": (seed + timedelta(minutes=30)).isoformat(),
            "rrule": "FREQ=WEEKLY;COUNT=2",
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=_auth(client._tok),
    )
    assert r2.status == 422


# ─── Phase C: capacity + request-to-join + waitlist ─────────────────────────


async def _seed_outsider_member(client, *, role="member"):
    """Outsider with auth + a membership row in sp-cal."""
    from socialhome.auth import sha256_token_hash

    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('bob', 'uid-bob', 'Bob', 0)",
    )
    raw = "bob-tok"
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('to-bob', 'uid-bob', 't', ?)",
        (sha256_token_hash(raw),),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-cal', ?, ?)",
        ("uid-bob", role),
    )
    return {"Authorization": f"Bearer {raw}"}


async def test_capacity_creates_event_with_capacity_field(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Limited",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "capacity": 5,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["capacity"] == 5


async def test_capped_event_member_rsvp_lands_in_pending_queue(client):
    await _seed_space(client)
    bob = await _seed_outsider_member(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Tiny",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "capacity": 5,
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    r2 = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=bob,
    )
    assert r2.status == 200
    counts = (await r2.json())["counts"]
    assert counts["requested"] == 1
    # Host fetches pending queue
    r3 = await client.get(
        f"/api/calendars/events/{eid}/pending",
        headers=_auth(client._tok),
    )
    assert r3.status == 200
    pending = (await r3.json())["pending"]
    assert len(pending) == 1
    assert pending[0]["user_id"] == "uid-bob"


async def test_approve_promotes_to_going(client):
    await _seed_space(client)
    bob = await _seed_outsider_member(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Tiny",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "capacity": 5,
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=bob,
    )
    r2 = await client.post(
        f"/api/calendars/events/{eid}/approve",
        json={"user_id": "uid-bob", "action": "approve"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    body = await r2.json()
    assert body["new_status"] == "going"
    # Pending queue now empty.
    r3 = await client.get(
        f"/api/calendars/events/{eid}/pending",
        headers=_auth(client._tok),
    )
    assert (await r3.json())["pending"] == []


async def test_deny_clears_request(client):
    await _seed_space(client)
    bob = await _seed_outsider_member(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Tiny",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "capacity": 5,
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=bob,
    )
    r2 = await client.post(
        f"/api/calendars/events/{eid}/approve",
        json={"user_id": "uid-bob", "action": "deny"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    counts = (await r2.json())["counts"]
    assert counts["requested"] == 0


# ─── Phase F: iCal export ───────────────────────────────────────────────────


async def test_event_ics_endpoint_returns_vcalendar(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Birthday",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    r2 = await client.get(
        f"/api/calendars/events/{eid}/export.ics",
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    assert r2.headers["Content-Type"].startswith("text/calendar")
    body = await r2.text()
    assert "BEGIN:VCALENDAR" in body
    assert f"UID:{eid}" in body
    assert "SUMMARY:Birthday" in body


async def test_event_ics_non_member_403(client):
    await _seed_space(client)
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Members only",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    outsider = await _seed_outsider(client)
    r2 = await client.get(
        f"/api/calendars/events/{eid}/export.ics",
        headers=outsider,
    )
    assert r2.status == 403


async def test_feed_token_lifecycle(client):
    """POST mints a token; the feed URL works; DELETE revokes it."""
    await _seed_space(client)
    # Mint a token
    r = await client.post(
        "/api/spaces/sp-cal/calendar/feed-token",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    token = body["token"]
    assert token
    # Create an event so the feed has content.
    now = datetime.now(timezone.utc)
    await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Feed test",
            "start": (now + timedelta(days=1)).isoformat(),
            "end": (now + timedelta(days=1, hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    # Subscribable feed works without auth, just the token.
    r2 = await client.get(f"/api/spaces/sp-cal/calendar/export.ics?token={token}")
    assert r2.status == 200
    feed_body = await r2.text()
    assert "BEGIN:VCALENDAR" in feed_body
    assert "SUMMARY:Feed test" in feed_body
    # Conditional GET — same ETag → 304.
    etag = r2.headers["ETag"]
    r3 = await client.get(
        f"/api/spaces/sp-cal/calendar/export.ics?token={token}",
        headers={"If-None-Match": etag},
    )
    assert r3.status == 304
    # Revoke
    r4 = await client.delete(
        "/api/spaces/sp-cal/calendar/feed-token",
        headers=_auth(client._tok),
    )
    assert r4.status == 200
    # Token now rejected.
    r5 = await client.get(f"/api/spaces/sp-cal/calendar/export.ics?token={token}")
    assert r5.status == 401


async def test_feed_token_required(client):
    """No token → 401."""
    await _seed_space(client)
    r = await client.get("/api/spaces/sp-cal/calendar/export.ics")
    assert r.status == 401


async def test_feed_token_for_wrong_space_rejected(client):
    """A token bound to space A can't be used to fetch space B's feed."""
    await _seed_space(client)
    # Create a second space + add the test user as a member.
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES('sp-other', 'Other', 'iid', 'admin', ?, 'household')",
        ("aa" * 32,),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) "
        "VALUES('sp-other', ?, 'admin')",
        (client._uid,),
    )
    r = await client.post(
        "/api/spaces/sp-cal/calendar/feed-token",
        json={},
        headers=_auth(client._tok),
    )
    token = (await r.json())["token"]
    # Try to use sp-cal's token on sp-other.
    r2 = await client.get(f"/api/spaces/sp-other/calendar/export.ics?token={token}")
    assert r2.status == 401


async def test_approve_non_creator_non_admin_403(client):
    """Random members can't approve other members' requests."""
    await _seed_space(client)
    bob = await _seed_outsider_member(client, role="member")
    # Add another non-admin member
    from socialhome.auth import sha256_token_hash

    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('carol', 'uid-carol', 'Carol', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('to-carol', 'uid-carol', 't', ?)",
        (sha256_token_hash("carol-tok"),),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) "
        "VALUES('sp-cal', 'uid-carol', 'member')",
    )
    carol_auth = {"Authorization": "Bearer carol-tok"}
    now = datetime.now(timezone.utc)
    r = await client.post(
        "/api/spaces/sp-cal/calendar/events",
        json={
            "summary": "Tiny",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "capacity": 5,
        },
        headers=_auth(client._tok),
    )
    eid = (await r.json())["id"]
    await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=bob,
    )
    # Carol (a regular member) tries to approve bob — must 403.
    r2 = await client.post(
        f"/api/calendars/events/{eid}/approve",
        json={"user_id": "uid-bob", "action": "approve"},
        headers=carol_auth,
    )
    assert r2.status == 403

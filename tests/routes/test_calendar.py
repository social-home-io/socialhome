"""Tests for socialhome.routes.calendar."""

import uuid as _uuid
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


async def test_event_cover_round_trips_create_edit_clear(client):
    """``cover_url`` is a tri-state field: ``null``/missing means "no
    cover", a string sets it, and an explicit ``null`` on PATCH
    clears it. The route + service preserve this discipline so
    editing a non-cover field doesn't accidentally drop the cover."""
    r = await client.post(
        "/api/calendars", json={"name": "C"}, headers=_auth(client._tok)
    )
    cid = (await r.json())["id"]
    now = datetime.now(timezone.utc)

    # 1. Create with a cover.
    r = await client.post(
        f"/api/calendars/{cid}/events",
        json={
            "summary": "Picnic",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
            "cover_url": "/api/media/picnic.webp",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    # Responses now carry a signed ``cover_url`` so the SPA can drop
    # the URL into ``<img src>`` without a Bearer token. The canonical
    # path under the signature is what we asserted on before signing
    # was wired in.
    assert body["cover_url"].split("?", 1)[0] == "/api/media/picnic.webp"
    eid = body["id"]

    # 2. Edit the summary only — cover stays.
    r2 = await client.patch(
        f"/api/calendars/events/{eid}",
        json={"summary": "Big Picnic"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    assert (await r2.json())["cover_url"].split("?", 1)[0] == "/api/media/picnic.webp"

    # 3. Replace the cover.
    r3 = await client.patch(
        f"/api/calendars/events/{eid}",
        json={"cover_url": "/api/media/picnic-2.webp"},
        headers=_auth(client._tok),
    )
    assert (await r3.json())["cover_url"].split("?", 1)[0] == "/api/media/picnic-2.webp"

    # 4. Clear the cover with explicit null.
    r4 = await client.patch(
        f"/api/calendars/events/{eid}",
        json={"cover_url": None},
        headers=_auth(client._tok),
    )
    assert (await r4.json())["cover_url"] is None


async def test_event_patch_attaches_client_event_uuid(client):
    """PATCH can stamp a ``client_event_uuid`` onto a legacy event so
    fanned-out copies share a group id (#327)."""
    r = await client.post(
        "/api/calendars", json={"name": "C"}, headers=_auth(client._tok)
    )
    cid = (await r.json())["id"]
    now = datetime.now(timezone.utc)
    r = await client.post(
        f"/api/calendars/{cid}/events",
        json={
            "summary": "Picnic",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["client_event_uuid"] is None
    eid = body["id"]

    grp = "11111111-2222-3333-4444-555555555555"
    r2 = await client.patch(
        f"/api/calendars/events/{eid}",
        json={"summary": "Picnic — group", "client_event_uuid": grp},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    assert (await r2.json())["client_event_uuid"] == grp


# ─── Space-scoped calendar + RSVP (§23.7) ─────────────────────────────────


def _future_seed(*, days: int = 1) -> datetime:
    """A recurring-event seed whose whole window is still in the future.

    Hard-coded calendar dates rot. ``CalendarService.set_rsvp`` enforces
    the Phase E past-event lock — an RSVP to an occurrence that has
    already ended is a write into the past and raises (422) — so a
    fixture pinned to a literal date starts failing the day its last
    occurrence passes. ``test_rsvp_recurring_per_occurrence`` was seeded
    at 2026-08-03 with ``FREQ=WEEKLY;COUNT=4`` and duly went red once
    2026-08-24 was behind us.

    Seconds and microseconds are zeroed so ``seed.isoformat()``
    round-trips through storage and ``expand_rrule`` byte-for-byte —
    the tests compare ``occurrence_at`` strings.
    """
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        second=0,
        microsecond=0,
    )


async def _seed_space(client):
    db = client._db
    # ``feature_calendar`` schema default is 0 — the 0008 migration
    # backfills existing rows to 1 and the SpaceFeatures dataclass
    # default is True, but raw INSERTs in test fixtures still need
    # an explicit value or the new route-level gate 403s.
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type, feature_calendar) "
        "VALUES('sp-cal', 'Cal', 'iid', 'admin', ?, 'household', 1)",
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
    seed = _future_seed()
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
    seed = _future_seed()
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
        "identity_public_key, space_type, feature_calendar) "
        "VALUES('sp-other', 'Other', 'iid', 'admin', ?, 'household', 1)",
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


async def _space(client) -> str:
    r = await client.post(
        "/api/spaces",
        json={"name": "Crew", "emoji": "📅"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    return (await r.json())["id"]


async def _now_iso(days_ahead: int = 1) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(days=days_ahead)).isoformat()


async def test_space_event_not_in_feed_by_default(client):
    """§23.15 — a space calendar event lives in the Calendar tab; it does
    NOT mirror to the feed unless the creator announces it."""
    h = _auth(client._tok)
    sid = await _space(client)
    start = await _now_iso()
    r = await client.post(
        f"/api/spaces/{sid}/calendar/events",
        json={"summary": "Standup", "start": start, "end": start},
        headers=h,
    )
    assert r.status == 201, await r.text()
    feed = await (await client.get(f"/api/spaces/{sid}/feed", headers=h)).json()
    assert all(p["type"] != "event" for p in feed), feed


async def test_space_event_in_feed_when_announced(client):
    """``announce_in_feed=True`` mirrors the event into the space feed."""
    h = _auth(client._tok)
    sid = await _space(client)
    start = await _now_iso()
    r = await client.post(
        f"/api/spaces/{sid}/calendar/events",
        json={
            "summary": "Launch party",
            "start": start,
            "end": start,
            "announce_in_feed": True,
        },
        headers=h,
    )
    assert r.status == 201, await r.text()
    feed = await (await client.get(f"/api/spaces/{sid}/feed", headers=h)).json()
    assert any(p["type"] == "event" for p in feed), feed


# ─── Server-authoritative fan-out copies ────────────────────────────────


async def _seed_member_calendar(client, *, username, cal_id):
    """Add a household member plus a personal calendar they own."""
    await client._db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, f"uid-{username}", username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO calendars(id, name, color, owner_username, calendar_type) "
        "VALUES(?,?,?,?,'personal')",
        (cal_id, username.title(), "#4a90e2", username),
    )
    return cal_id


async def test_event_copies_are_visibility_independent(client):
    """REGRESSION: an event fanned out to three member calendars reports
    ALL three copies even when only one calendar was queried.

    The SPA used to infer the sibling set from whichever calendars were
    visible in the agenda, so editing a shared event POSTed duplicates.
    The server is the authority now — querying calendar A alone still
    yields B's and C's copies.
    """
    cal_a = await _seed_member_calendar(client, username="anna", cal_id="cal-a")
    cal_b = await _seed_member_calendar(client, username="lina", cal_id="cal-b")
    cal_c = await _seed_member_calendar(client, username="max", cal_id="cal-c")
    now = datetime.now(timezone.utc)
    grp = _uuid.uuid4().hex
    for cid in (cal_a, cal_b, cal_c):
        r = await client.post(
            f"/api/calendars/{cid}/events",
            json={
                "summary": "Family dinner",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "client_event_uuid": grp,
            },
            headers=_auth(client._tok),
        )
        assert r.status == 201
        assert {c["calendar_id"] for c in (await r.json())["copies"]} <= {
            cal_a,
            cal_b,
            cal_c,
        }

    start_q = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
    end_q = (now + timedelta(hours=2)).replace(tzinfo=None).isoformat() + "Z"
    r = await client.get(
        f"/api/calendars/{cal_a}/events?start={start_q}&end={end_q}",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    events = await r.json()
    assert len(events) == 1
    copies = events[0]["copies"]
    assert {c["calendar_id"] for c in copies} == {cal_a, cal_b, cal_c}
    assert {c["owner_username"] for c in copies} == {"anna", "lina", "max"}
    assert all(c["event_id"] for c in copies)

    # Single-event reads carry the same authoritative set.
    eid = events[0]["id"]
    r = await client.get(f"/api/calendars/events/{eid}", headers=_auth(client._tok))
    assert r.status == 200
    assert {c["calendar_id"] for c in (await r.json())["copies"]} == {
        cal_a,
        cal_b,
        cal_c,
    }
    r = await client.patch(
        f"/api/calendars/events/{eid}",
        json={"summary": "Family dinner — 7pm"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert {c["calendar_id"] for c in (await r.json())["copies"]} == {
        cal_a,
        cal_b,
        cal_c,
    }


async def test_recurring_event_copies_carry_stored_ids(client):
    """A recurring event expands into synthetic ``{id}@{iso}`` occurrence
    ids, but ``copies`` must carry the STORED row ids — the SPA PATCHes
    those, and an ``@``-suffixed id 404s."""
    cal_a = await _seed_member_calendar(client, username="rita", cal_id="cal-r1")
    cal_b = await _seed_member_calendar(client, username="rudi", cal_id="cal-r2")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    grp = _uuid.uuid4().hex
    stored_ids = set()
    for cid in (cal_a, cal_b):
        r = await client.post(
            f"/api/calendars/{cid}/events",
            json={
                "summary": "Weekly sync",
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "rrule": "FREQ=DAILY;COUNT=5",
                "client_event_uuid": grp,
            },
            headers=_auth(client._tok),
        )
        assert r.status == 201
        stored_ids.add((await r.json())["id"])

    start_q = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
    end_q = (now + timedelta(days=3)).replace(tzinfo=None).isoformat() + "Z"
    r = await client.get(
        f"/api/calendars/{cal_a}/events?start={start_q}&end={end_q}",
        headers=_auth(client._tok),
    )
    events = await r.json()
    assert len(events) > 1, "expected expanded occurrences"
    # At least one returned row is a synthetic occurrence...
    assert any("@" in e["id"] for e in events)
    # ...but every copy id is a stored row id, never a synthetic one.
    for e in events:
        ids = {c["event_id"] for c in e["copies"]}
        assert ids == stored_ids, (e["id"], ids)
        assert not any("@" in i for i in ids)


async def test_remote_invite_mirror_never_appears_in_copies(client):
    """A ``remote_invite`` row carrying the peer's ``client_event_uuid``
    is not ours to edit — it must stay out of ``copies``."""
    cal_a = await _seed_member_calendar(client, username="nina", cal_id="cal-n1")
    now = datetime.now(timezone.utc)
    grp = _uuid.uuid4().hex
    r = await client.post(
        f"/api/calendars/{cal_a}/events",
        json={
            "summary": "Ours",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "client_event_uuid": grp,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    local_id = (await r.json())["id"]

    cal_m = await _seed_member_calendar(client, username="nolan", cal_id="cal-n2")
    await client._db.enqueue(
        "INSERT INTO calendar_events(id, calendar_id, summary, start_dt,"
        " end_dt, created_by, origin, client_event_uuid, tz)"
        " VALUES(?,?,?,?,?,?,'remote_invite',?,'UTC')",
        (
            "mirror-1",
            cal_m,
            "Theirs",
            now.isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            "uid-nolan",
            grp,
        ),
    )

    start_q = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
    end_q = (now + timedelta(hours=2)).replace(tzinfo=None).isoformat() + "Z"
    # Guard the fixture itself: the mirror row really is there and really
    # carries the same group uuid, so the assertion below is meaningful.
    r = await client.get(
        f"/api/calendars/{cal_m}/events?start={start_q}&end={end_q}",
        headers=_auth(client._tok),
    )
    mirrors = await r.json()
    assert [e["id"] for e in mirrors] == ["mirror-1"]
    assert mirrors[0]["client_event_uuid"] == grp
    # The mirror row reports NO copies of its own — see
    # ``test_remote_invite_mirror_reports_no_copies``.
    assert mirrors[0]["copies"] == []

    r = await client.get(
        f"/api/calendars/{cal_a}/events?start={start_q}&end={end_q}",
        headers=_auth(client._tok),
    )
    events = await r.json()
    assert [c["event_id"] for c in events[0]["copies"]] == [local_id]


async def test_remote_invite_mirror_reports_no_copies(client):
    """A ``remote_invite`` mirror never gets a copies lookup.

    ``federation_inbound.personal_calendar`` stores the PEER's
    ``client_event_uuid`` verbatim — unvalidated and unbounded. On a
    collision with one of our own group uuids the mirror would render
    our household members' chips and seed the edit dialog with OUR
    rows, so a save would rewrite events the peer has no business
    touching. Only ``origin='local'`` rows get a copies lookup.
    """
    cal_a = await _seed_member_calendar(client, username="orin", cal_id="cal-o1")
    now = datetime.now(timezone.utc)
    grp = _uuid.uuid4().hex
    r = await client.post(
        f"/api/calendars/{cal_a}/events",
        json={
            "summary": "Ours",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "client_event_uuid": grp,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201

    cal_m = await _seed_member_calendar(client, username="opal", cal_id="cal-o2")
    # The collision: the peer's row carries the SAME uuid as our group.
    await client._db.enqueue(
        "INSERT INTO calendar_events(id, calendar_id, summary, start_dt,"
        " end_dt, created_by, origin, client_event_uuid, tz)"
        " VALUES(?,?,?,?,?,?,'remote_invite',?,'UTC')",
        (
            "mirror-o",
            cal_m,
            "Theirs",
            now.isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            "uid-opal",
            grp,
        ),
    )

    start_q = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
    end_q = (now + timedelta(hours=2)).replace(tzinfo=None).isoformat() + "Z"
    r = await client.get(
        f"/api/calendars/{cal_m}/events?start={start_q}&end={end_q}",
        headers=_auth(client._tok),
    )
    mirrors = await r.json()
    assert [e["id"] for e in mirrors] == ["mirror-o"]
    # Fixture guard: the collision really is set up.
    assert mirrors[0]["client_event_uuid"] == grp
    assert mirrors[0]["copies"] == []


async def test_event_without_client_event_uuid_has_empty_copies(client):
    """Legacy / ICS-imported rows carry no group uuid → ``copies: []``."""
    cal = await _seed_member_calendar(client, username="olga", cal_id="cal-o1")
    now = datetime.now(timezone.utc)
    r = await client.post(
        f"/api/calendars/{cal}/events",
        json={
            "summary": "Solo",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["copies"] == []
    r = await client.get(
        f"/api/calendars/events/{body['id']}", headers=_auth(client._tok)
    )
    assert (await r.json())["copies"] == []


async def test_space_event_payload_has_empty_copies(client):
    """Space events live in ``space_calendar_events`` and have no
    household fan-out — ``copies`` is always empty for them."""
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
    body = await r.json()
    assert body["copies"] == []
    r = await client.get(
        f"/api/calendars/events/{body['id']}", headers=_auth(client._tok)
    )
    assert r.status == 200
    assert (await r.json())["copies"] == []


async def test_post_twice_with_same_client_event_uuid_yields_one_row(client):
    """REGRESSION (endpoint level): re-POSTing a shared event to the same
    calendar with the same ``client_event_uuid`` updates the existing row
    (still ``201``) instead of minting a duplicate."""
    cal = await _seed_member_calendar(client, username="petra", cal_id="cal-p1")
    now = datetime.now(timezone.utc)
    grp = _uuid.uuid4().hex
    payload = {
        "summary": "Book club",
        "start": now.isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "client_event_uuid": grp,
    }
    r1 = await client.post(
        f"/api/calendars/{cal}/events", json=payload, headers=_auth(client._tok)
    )
    assert r1.status == 201
    first = await r1.json()
    r2 = await client.post(
        f"/api/calendars/{cal}/events",
        json={**payload, "summary": "Book club — moved"},
        headers=_auth(client._tok),
    )
    assert r2.status == 201
    second = await r2.json()
    assert second["id"] == first["id"]
    assert second["summary"] == "Book club — moved"

    start_q = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
    end_q = (now + timedelta(hours=2)).replace(tzinfo=None).isoformat() + "Z"
    r = await client.get(
        f"/api/calendars/{cal}/events?start={start_q}&end={end_q}",
        headers=_auth(client._tok),
    )
    assert len(await r.json()) == 1


async def test_patch_with_uuid_taken_on_same_calendar_is_422(client):
    """PATCHing a ``client_event_uuid`` that another local row on the same
    calendar already holds violates ``ux_calendar_events_fanout`` — the
    endpoint answers 422, never a 500 with a stack trace."""
    cal = await _seed_member_calendar(client, username="quinn", cal_id="cal-q1")
    now = datetime.now(timezone.utc)
    grp = _uuid.uuid4().hex
    r = await client.post(
        f"/api/calendars/{cal}/events",
        json={
            "summary": "Taken",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "client_event_uuid": grp,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.post(
        f"/api/calendars/{cal}/events",
        json={
            "summary": "Legacy",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    legacy_id = (await r.json())["id"]

    r = await client.patch(
        f"/api/calendars/events/{legacy_id}",
        json={"client_event_uuid": grp},
        headers=_auth(client._tok),
    )
    assert r.status == 422, await r.text()

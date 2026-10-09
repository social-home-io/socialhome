"""Space-calendar scope + write-gate tests for socialhome.routes.calendar.

Every space-calendar route checks membership of the PATH space and then
acts only on that space's events (§24.11): an event id from another
space is 404 and its row is untouched. Writes (create / edit / delete /
RSVP / host approval) additionally need a writable seat — read-only
subscribers are 403 — in a non-archived space (403). The household
``/api/calendars/events/{id}`` PATCH / DELETE only ever reach personal
calendar rows, never a space event (404).
"""

from datetime import datetime, timedelta, timezone

from socialhome.auth import sha256_token_hash

from .conftest import _auth


async def _seed_space(client, sid: str, user_id: str, role: str) -> None:
    await client._db.enqueue(
        "INSERT OR IGNORE INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, feature_calendar)"
        " VALUES(?, ?, 'inst', 'admin', ?, 'household', 1)",
        (sid, sid, "ab" * 32),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, user_id, role),
    )


async def _add_user(client, username: str, user_id: str) -> dict:
    token = f"{username}-tok"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?, ?, ?, 0)",
        (username, user_id, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES(?, ?, 't', ?)",
        (f"t-{username}", user_id, sha256_token_hash(token)),
    )
    return {"Authorization": f"Bearer {token}"}


def _window() -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = (now - timedelta(days=1)).isoformat() + "Z"
    end = (now + timedelta(days=3)).isoformat() + "Z"
    return f"start={start}&end={end}"


async def _create_event(client, sid: str, headers: dict, **extra) -> str:
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    r = await client.post(
        f"/api/spaces/{sid}/calendar/events",
        json={
            "summary": "Space B party",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            **extra,
        },
        headers=headers,
    )
    assert r.status == 201, await r.text()
    return (await r.json())["id"]


async def _event_rows(client, sid: str) -> list[tuple]:
    rows = await client._db.fetchall(
        "SELECT id, space_id, summary, start_dt, capacity FROM space_calendar_events"
        " WHERE space_id=? ORDER BY id",
        (sid,),
    )
    return [tuple(r) for r in rows]


async def _rsvp_rows(client) -> list[tuple]:
    rows = await client._db.fetchall(
        "SELECT event_id, user_id, status FROM space_calendar_rsvps"
        " ORDER BY event_id, user_id",
    )
    return [tuple(r) for r in rows]


async def _cross_space_env(client):
    """Admin owns ``sp-b`` (one event); bob is a member of ``sp-a`` only."""
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    bob = await _add_user(client, "bob", "bob-id")
    await _seed_space(client, "sp-a", "bob-id", "member")
    eid = await _create_event(client, "sp-b", admin)
    return bob, eid


# ─── Cross-space IDOR ────────────────────────────────────────────────────


async def test_space_event_from_another_space_is_404_and_untouched(client):
    bob, eid = await _cross_space_env(client)
    before = await _event_rows(client, "sp-b")
    r = await client.patch(
        f"/api/spaces/sp-a/calendar/events/{eid}",
        json={"summary": "pwned", "capacity": 0},
        headers=bob,
    )
    assert r.status == 404
    r = await client.delete(f"/api/spaces/sp-a/calendar/events/{eid}", headers=bob)
    assert r.status == 404
    assert await _event_rows(client, "sp-b") == before
    assert len(before) == 1


async def test_space_event_listing_is_scoped_to_the_path_space(client):
    bob, eid = await _cross_space_env(client)
    r = await client.get(f"/api/spaces/sp-a/calendar/events?{_window()}", headers=bob)
    assert r.status == 200
    assert [e["id"] for e in await r.json()] == []


# ─── Non-member reads ────────────────────────────────────────────────────


async def test_space_event_list_non_member_403(client):
    bob, eid = await _cross_space_env(client)
    r = await client.get(f"/api/spaces/sp-b/calendar/events?{_window()}", headers=bob)
    assert r.status == 403
    assert "Space B party" not in await r.text()
    # Membership is checked before the query-param / feature checks.
    r = await client.get("/api/spaces/sp-b/calendar/events", headers=bob)
    assert r.status == 403


async def test_space_event_list_member_200(client):
    await _seed_space(client, "sp-b", client._uid, "owner")
    eid = await _create_event(client, "sp-b", _auth(client._tok))
    r = await client.get(
        f"/api/spaces/sp-b/calendar/events?{_window()}", headers=_auth(client._tok)
    )
    assert r.status == 200
    assert [e["id"] for e in await r.json()] == [eid]


async def test_space_event_list_feature_off_403(client):
    await _seed_space(client, "sp-b", client._uid, "owner")
    await client._db.enqueue("UPDATE spaces SET feature_calendar=0 WHERE id='sp-b'")
    r = await client.get(
        f"/api/spaces/sp-b/calendar/events?{_window()}", headers=_auth(client._tok)
    )
    assert r.status == 403


async def test_rsvp_list_non_member_404(client):
    bob, eid = await _cross_space_env(client)
    r = await client.get(f"/api/calendars/events/{eid}/rsvps", headers=bob)
    assert r.status == 404
    assert client._uid not in await r.text()


async def test_rsvp_list_unknown_event_404(client):
    r = await client.get("/api/calendars/events/nope/rsvps", headers=_auth(client._tok))
    assert r.status == 404


async def test_event_read_non_member_404(client):
    bob, eid = await _cross_space_env(client)
    r = await client.get(f"/api/calendars/events/{eid}", headers=bob)
    assert r.status == 404
    assert "Space B party" not in await r.text()


def _id_only_calls(client, eid: str, headers: dict) -> list:
    base = f"/api/calendars/events/{eid}"
    return [
        client.get(base, headers=headers),
        client.post(f"{base}/rsvp", json={"status": "going"}, headers=headers),
        client.delete(f"{base}/rsvp", headers=headers),
        client.get(f"{base}/rsvps", headers=headers),
        client.get(f"{base}/pending", headers=headers),
        client.get(f"{base}/reminders", headers=headers),
        client.post(f"{base}/reminders", json={"minutes_before": 5}, headers=headers),
        client.delete(f"{base}/reminders?minutes_before=5", headers=headers),
        client.post(
            f"{base}/approve",
            json={"user_id": "x", "action": "deny"},
            headers=headers,
        ),
        client.get(f"{base}/export.ics", headers=headers),
    ]


async def test_id_only_event_routes_are_no_existence_oracle(client):
    """A non-member gets exactly what an unknown id gets — 404 with the
    same body — on every id-only route, so the response never confirms
    that an event id exists in a space the caller isn't in."""
    bob, eid = await _cross_space_env(client)
    rows_before = await _event_rows(client, "sp-b")
    rsvps_before = await _rsvp_rows(client)
    for real, ghost in zip(
        _id_only_calls(client, eid, bob),
        _id_only_calls(client, "no-such-event", bob),
        strict=True,
    ):
        r_real, r_ghost = await real, await ghost
        assert r_real.status == 404, (r_real.method, r_real.url, await r_real.text())
        assert r_ghost.status == 404
        assert await r_real.json() == await r_ghost.json()
    assert await _event_rows(client, "sp-b") == rows_before
    assert await _rsvp_rows(client) == rsvps_before


# ─── Subscribers read, never write ───────────────────────────────────────


async def _subscriber_env(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    carol = await _add_user(client, "carol", "carol-id")
    await _seed_space(client, "sp-b", "carol-id", "subscriber")
    eid = await _create_event(client, "sp-b", admin)
    return carol, eid


async def test_space_calendar_subscriber_reads_but_never_writes(client):
    carol, eid = await _subscriber_env(client)
    r = await client.get(f"/api/spaces/sp-b/calendar/events?{_window()}", headers=carol)
    assert r.status == 200
    assert [e["id"] for e in await r.json()] == [eid]
    assert (
        await client.get(f"/api/calendars/events/{eid}", headers=carol)
    ).status == 200
    events_before = await _event_rows(client, "sp-b")
    rsvps_before = await _rsvp_rows(client)
    start = datetime.now(timezone.utc) + timedelta(hours=5)
    writes = [
        client.post(
            "/api/spaces/sp-b/calendar/events",
            json={
                "summary": "planted",
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
            },
            headers=carol,
        ),
        client.patch(
            f"/api/spaces/sp-b/calendar/events/{eid}",
            json={"summary": "X"},
            headers=carol,
        ),
        client.delete(f"/api/spaces/sp-b/calendar/events/{eid}", headers=carol),
        client.post(
            f"/api/calendars/events/{eid}/rsvp", json={"status": "going"}, headers=carol
        ),
        client.delete(f"/api/calendars/events/{eid}/rsvp", headers=carol),
    ]
    for req in writes:
        r = await req
        assert r.status == 403, await r.text()
    assert await _event_rows(client, "sp-b") == events_before
    assert await _rsvp_rows(client) == rsvps_before


async def test_subscriber_cannot_approve_even_as_event_creator(client):
    """Carol created a capped event as a member, then was demoted to
    subscriber. The creator check alone would let her approve; the
    writer gate must refuse (403) before the approval is looked up."""
    await _seed_space(client, "sp-b", client._uid, "owner")
    carol = await _add_user(client, "carol", "carol-id")
    await _seed_space(client, "sp-b", "carol-id", "member")
    eid = await _create_event(client, "sp-b", carol, capacity=1)
    r = await client.post(
        f"/api/calendars/events/{eid}/rsvp",
        json={"status": "going"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["counts"]["requested"] == 1
    await client._db.enqueue(
        "UPDATE space_members SET role='subscriber'"
        " WHERE space_id='sp-b' AND user_id='carol-id'"
    )
    rsvps_before = await _rsvp_rows(client)
    r = await client.post(
        f"/api/calendars/events/{eid}/approve",
        json={"user_id": client._uid, "action": "approve"},
        headers=carol,
    )
    assert r.status == 403, await r.text()
    assert await _rsvp_rows(client) == rsvps_before


async def test_subscriber_rsvp_refusal_names_rsvp(client):
    carol, eid = await _subscriber_env(client)
    r = await client.post(
        f"/api/calendars/events/{eid}/rsvp", json={"status": "going"}, headers=carol
    )
    assert r.status == 403
    text = await r.text()
    assert "RSVP" in text
    assert "edit the calendar" not in text


async def test_subscriber_can_manage_own_reminders(client):
    carol, eid = await _subscriber_env(client)
    base = f"/api/calendars/events/{eid}/reminders"
    r = await client.post(base, json={"minutes_before": 15}, headers=carol)
    assert r.status == 201, await r.text()
    r = await client.get(base, headers=carol)
    assert r.status == 200
    assert [m["minutes_before"] for m in (await r.json())["reminders"]] == [15]
    r = await client.delete(f"{base}?minutes_before=15", headers=carol)
    assert r.status == 200
    r = await client.get(base, headers=carol)
    assert (await r.json())["reminders"] == []


async def test_space_calendar_member_writes_are_allowed(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    bob = await _add_user(client, "bob", "bob-id")
    await _seed_space(client, "sp-b", "bob-id", "member")
    eid = await _create_event(client, "sp-b", bob)
    r = await client.patch(
        f"/api/spaces/sp-b/calendar/events/{eid}",
        json={"summary": "Renamed"},
        headers=bob,
    )
    assert r.status == 200
    assert (await r.json())["summary"] == "Renamed"
    r = await client.post(
        f"/api/calendars/events/{eid}/rsvp", json={"status": "going"}, headers=admin
    )
    assert r.status == 200
    r = await client.delete(f"/api/spaces/sp-b/calendar/events/{eid}", headers=bob)
    assert r.status == 200
    # A content-free tombstone stays (migration 0085); its RSVPs are gone.
    assert await _event_rows(client, "sp-b") == [(eid, "sp-b", "", "", None)]
    assert await _rsvp_rows(client) == []


# ─── Archived spaces are read-only ───────────────────────────────────────


async def test_archived_space_calendar_is_read_only(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    eid = await _create_event(client, "sp-b", admin)
    await client._db.enqueue("UPDATE spaces SET archived=1 WHERE id='sp-b'")
    events_before = await _event_rows(client, "sp-b")
    rsvps_before = await _rsvp_rows(client)
    start = datetime.now(timezone.utc) + timedelta(hours=5)
    writes = [
        client.post(
            "/api/spaces/sp-b/calendar/events",
            json={
                "summary": "late",
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
            },
            headers=admin,
        ),
        client.patch(
            f"/api/spaces/sp-b/calendar/events/{eid}",
            json={"summary": "X"},
            headers=admin,
        ),
        client.delete(f"/api/spaces/sp-b/calendar/events/{eid}", headers=admin),
        client.post(
            f"/api/calendars/events/{eid}/rsvp", json={"status": "maybe"}, headers=admin
        ),
        client.delete(f"/api/calendars/events/{eid}/rsvp", headers=admin),
        client.post(
            f"/api/calendars/events/{eid}/approve",
            json={"user_id": "x", "action": "deny"},
            headers=admin,
        ),
    ]
    for req in writes:
        r = await req
        assert r.status == 403, await r.text()
    assert await _event_rows(client, "sp-b") == events_before
    assert await _rsvp_rows(client) == rsvps_before
    # Reads keep working on an archived space.
    r = await client.get(f"/api/spaces/sp-b/calendar/events?{_window()}", headers=admin)
    assert r.status == 200
    assert [e["id"] for e in await r.json()] == [eid]


# ─── Household endpoint never reaches a space event ─────────────────────


async def test_household_event_endpoint_on_space_event_is_404(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    eid = await _create_event(client, "sp-b", admin)
    before = await _event_rows(client, "sp-b")
    r = await client.patch(
        f"/api/calendars/events/{eid}", json={"summary": "pwned"}, headers=admin
    )
    assert r.status == 404
    r = await client.delete(f"/api/calendars/events/{eid}", headers=admin)
    assert r.status == 404
    assert await _event_rows(client, "sp-b") == before


async def test_household_member_may_edit_another_members_personal_event(client):
    """§23.60: the household is the unit of trust for personal calendars —
    any active household member may edit / delete events on any member's
    calendar. Guard the documented rule so the space fix doesn't regress it."""
    admin = _auth(client._tok)
    bob = await _add_user(client, "bob", "bob-id")
    r = await client.post("/api/calendars", json={"name": "Admin"}, headers=admin)
    cid = (await r.json())["id"]
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    r = await client.post(
        f"/api/calendars/{cid}/events",
        json={
            "summary": "Dentist",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
        },
        headers=admin,
    )
    eid = (await r.json())["id"]
    r = await client.patch(
        f"/api/calendars/events/{eid}", json={"summary": "Moved"}, headers=bob
    )
    assert r.status == 200
    r = await client.delete(f"/api/calendars/events/{eid}", headers=bob)
    assert r.status == 200


# ─── Host approval + pending list ────────────────────────────────────────


async def test_pending_list_plain_member_403(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    eid = await _create_event(client, "sp-b", admin, capacity=2)
    bob = await _add_user(client, "bob", "bob-id")
    await _seed_space(client, "sp-b", "bob-id", "member")
    r = await client.get(f"/api/calendars/events/{eid}/pending", headers=bob)
    assert r.status == 403
    r = await client.get(f"/api/calendars/events/{eid}/pending", headers=admin)
    assert r.status == 200


# ─── Accepted remote invites ─────────────────────────────────────────────


async def _accepted_invite(client, sid: str, user_id: str) -> None:
    """The invitee-side row a §D1b accept leaves behind."""
    await client._db.enqueue(
        "INSERT INTO space_invitations(id, space_id, invited_by, invited_user_id,"
        " remote_user_id, remote_instance_id, status, expires_at)"
        " VALUES(?, ?, 'host-user', ?, ?, 'peer-host', 'accepted',"
        " '2099-01-01T00:00:00+00:00')",
        (f"inv-{user_id}", sid, user_id, user_id),
    )


async def test_remote_invite_member_can_rsvp(client):
    """Accepting a remote invite seats a ``space_members`` row; that row
    is what lets the invitee RSVP (200), and archive still freezes it."""
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    eid = await _create_event(client, "sp-b", admin)
    dave = await _add_user(client, "dave", "dave-id")
    await _accepted_invite(client, "sp-b", "dave-id")
    await _seed_space(client, "sp-b", "dave-id", "member")
    rsvp = f"/api/calendars/events/{eid}/rsvp"
    r = await client.post(rsvp, json={"status": "going"}, headers=dave)
    assert r.status == 200, await r.text()
    assert ("dave-id", "going") in [(u, st) for _e, u, st in await _rsvp_rows(client)]
    r = await client.delete(rsvp, headers=dave)
    assert r.status == 200
    assert "dave-id" not in [u for _e, u, _st in await _rsvp_rows(client)]
    await client._db.enqueue("UPDATE spaces SET archived=1 WHERE id='sp-b'")
    r = await client.post(rsvp, json={"status": "going"}, headers=dave)
    assert r.status == 403
    r = await client.delete(rsvp, headers=dave)
    assert r.status == 403
    assert "dave-id" not in [u for _e, u, _st in await _rsvp_rows(client)]


async def test_accepted_invite_without_membership_grants_nothing(client):
    """A member who left or was removed keeps their accepted invitation
    row. It must not keep them reading or RSVPing (the old
    ``is_user_remote_member`` fallback did)."""
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    eid = await _create_event(client, "sp-b", admin)
    dave = await _add_user(client, "dave", "dave-id")
    await _accepted_invite(client, "sp-b", "dave-id")
    rsvps_before = await _rsvp_rows(client)
    for req in _id_only_calls(client, eid, dave):
        r = await req
        assert r.status == 404, (r.method, r.url, await r.text())
    assert await _rsvp_rows(client) == rsvps_before


# ─── iCal export + feed honour the calendar feature ──────────────────────


async def test_ics_export_and_feed_honour_calendar_feature(client):
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    eid = await _create_event(client, "sp-b", admin)
    r = await client.post("/api/spaces/sp-b/calendar/feed-token", headers=admin)
    assert r.status == 201
    token = (await r.json())["token"]
    feed = f"/api/spaces/sp-b/calendar/export.ics?token={token}"
    assert (
        await client.get(f"/api/calendars/events/{eid}/export.ics", headers=admin)
    ).status == 200
    assert (await client.get(feed)).status == 200
    await client._db.enqueue("UPDATE spaces SET feature_calendar=0 WHERE id='sp-b'")
    r = await client.get(f"/api/calendars/events/{eid}/export.ics", headers=admin)
    assert r.status == 403
    assert (await client.get(feed)).status == 403
    r = await client.post("/api/spaces/sp-b/calendar/feed-token", headers=admin)
    assert r.status == 403
    # Revoking still works with the feature off (a leaked token can be killed).
    r = await client.delete("/api/spaces/sp-b/calendar/feed-token", headers=admin)
    assert r.status == 200
    await client._db.enqueue("UPDATE spaces SET feature_calendar=1 WHERE id='sp-b'")
    assert (await client.get(feed)).status == 401


async def test_event_read_carries_can_rsvp_hint(client):
    """``can_rsvp`` lets the SPA hide RSVP controls the server would 403."""
    admin = _auth(client._tok)
    await _seed_space(client, "sp-b", client._uid, "owner")
    carol = await _add_user(client, "carol", "carol-id")
    await _seed_space(client, "sp-b", "carol-id", "subscriber")
    eid = await _create_event(client, "sp-b", admin)
    r = await client.get(f"/api/calendars/events/{eid}", headers=admin)
    assert (await r.json())["can_rsvp"] is True
    r = await client.get(f"/api/calendars/events/{eid}", headers=carol)
    assert r.status == 200
    assert (await r.json())["can_rsvp"] is False
    await client._db.enqueue("UPDATE spaces SET archived=1 WHERE id='sp-b'")
    r = await client.get(f"/api/calendars/events/{eid}", headers=admin)
    assert (await r.json())["can_rsvp"] is False

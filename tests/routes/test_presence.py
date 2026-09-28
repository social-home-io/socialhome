"""Tests for socialhome.routes.presence.

Wire-contract guards: `POST /api/presence/location` must accept the
body shape documented in spec §23.8.5 so the forthcoming
``ha-integration/`` component can push location updates without a
silent field-name drop. The endpoint derives :class:`PresenceState`
from ``zone_name`` and returns 204.
"""

from .conftest import _auth


async def test_list_presence(client):
    """GET /api/presence returns the presence list."""
    r = await client.get("/api/presence", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert isinstance(body, list)


async def test_list_presence_includes_online_status_fields(client):
    """Each row must carry the session-presence triple so the SPA can
    render the green/amber dot without an extra round-trip."""
    r = await client.get("/api/presence", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    if not body:  # household with no presence rows yet — still a valid response
        return
    row = body[0]
    assert "is_online" in row
    assert "is_idle" in row
    assert "last_seen_at" in row
    assert isinstance(row["is_online"], bool)
    assert isinstance(row["is_idle"], bool)


async def test_update_location_spec_compliant_payload_returns_204(client):
    """Spec §23.8.5: POST with latitude/longitude/accuracy_m/zone_name → 204.

    The DB row must carry the 4dp-truncated coordinates so the
    downstream realtime / federation fan-out sees the intended value.
    """
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "latitude": 52.37654321,
            "longitude": 4.89567890,
            "accuracy_m": 12.5,
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204

    row = await client._db.fetchone(
        "SELECT latitude, longitude, gps_accuracy_m, zone_name, state "
        "FROM presence WHERE username=?",
        ("admin",),
    )
    assert row is not None
    assert abs(row["latitude"] - 52.3765) < 1e-6
    assert abs(row["longitude"] - 4.8957) < 1e-6
    assert row["gps_accuracy_m"] == 12.5
    assert row["zone_name"] == "home"
    assert row["state"] == "home"


async def test_update_location_zone_name_home_derives_home_state(client):
    """zone_name='home' → state='home'."""
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT state FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["state"] == "home"


async def test_update_location_named_zone_derives_zone_state(client):
    """Any other non-empty zone_name → state='zone'."""
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": "Makers Space",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT state, zone_name FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["state"] == "zone"
    assert row["zone_name"] == "Makers Space"


async def test_update_location_null_zone_derives_away_state(client):
    """zone_name=null → state='away'."""
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": None,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT state FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["state"] == "away"


async def test_update_location_explicit_state_overrides_derivation(client):
    """An explicit ``state`` in the body wins over zone-based derivation.

    Used by manual/debug callers that want to force a state regardless
    of what zone_name implies. The ha-integration should not send this
    field — but tolerating it keeps operator tools simple.
    """
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": "home",
            "state": "away",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT state FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["state"] == "away"


async def test_update_location_accuracy_gate_nulls_coords(client):
    """accuracy_m > 500 nulls coordinates but keeps the zone name."""
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "latitude": 52.37,
            "longitude": 4.89,
            "accuracy_m": 750.0,
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT latitude, longitude, zone_name FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["zone_name"] == "home"


async def test_update_location_missing_username_422(client):
    """A body without username is a client error — helpful for debugging."""
    r = await client.post(
        "/api/presence/location",
        json={
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_update_location_admin_can_push_for_other_user(client):
    """Admin bearer (HA-integration's auto-provisioned token) can push
    presence on behalf of any household member — that's how one
    integration token covers every person.* entity."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.crypto import derive_user_id

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    target_uid = derive_user_id(pk_bytes, "lily")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("lily", target_uid, "Lily"),
    )

    r = await client.post(
        "/api/presence/location",
        json={"username": "lily", "zone_name": "home"},
        headers=_auth(client._tok),  # admin token from conftest
    )
    assert r.status == 204


async def test_update_location_non_admin_self_push_allowed(client):
    """A non-admin can push their OWN row — mobile clients / first-
    party SPA presence rely on this path."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "bob")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("bob", uid, "Bob"),
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-bob", uid, "t", sha256_token_hash(raw)),
    )
    r = await client.post(
        "/api/presence/location",
        json={"username": "bob", "zone_name": "home"},
        headers=_auth(raw),
    )
    assert r.status == 204


async def test_update_location_non_admin_cross_user_push_403(client):
    """A non-admin can NOT push someone else's presence — closes the
    cross-user spoof hole."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    bob_uid = derive_user_id(pk_bytes, "bob")
    lily_uid = derive_user_id(pk_bytes, "lily")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("bob", bob_uid, "Bob"),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("lily", lily_uid, "Lily"),
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-bob", bob_uid, "t", sha256_token_hash(raw)),
    )
    r = await client.post(
        "/api/presence/location",
        json={"username": "lily", "zone_name": "home"},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_presence_drops_blocked_household_member(client):
    """A blocked user is filtered out of the caller's /api/presence list."""
    # Seed a second household member with a presence row.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob-presence", "Bob"),
    )
    await client.post(
        "/api/presence/location",
        json={"username": "bob", "zone_name": "home"},
        headers=_auth(client._tok),
    )
    # Without a block, bob is in the list.
    body = await (await client.get("/api/presence", headers=_auth(client._tok))).json()
    assert any(p["user_id"] == "uid-bob-presence" for p in body)
    # After admin blocks bob, bob disappears for admin.
    r = await client.post(
        "/api/blocks",
        json={"user_id": "uid-bob-presence"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await (await client.get("/api/presence", headers=_auth(client._tok))).json()
    assert not any(p["user_id"] == "uid-bob-presence" for p in body)


async def test_update_location_unknown_username_404(client):
    """Pushing presence for a username that does not exist in ``users``
    must return 404, not the previous opaque 500 from the FK violation
    on ``presence.username -> users.username``."""
    r = await client.post(
        "/api/presence/location",
        json={"username": "nobody", "zone_name": "home"},
        headers=_auth(client._tok),  # admin token from conftest
    )
    assert r.status == 404
    body = await r.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_list_presence_carries_status(client):
    """Each row carries the member's emoji + text status (null when unset),
    so the Presence page can show it and refetch on ``user.status_changed``."""
    await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "latitude": 1.0,
            "longitude": 2.0,
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    rows = await (await client.get("/api/presence", headers=_auth(client._tok))).json()
    assert [r["status"] for r in rows if r["username"] == "admin"] == [None]

    r = await client.patch(
        "/api/me",
        json={"status_emoji": "🎧", "status_text": "Focus", "status_clear_after": "1h"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    rows = await (await client.get("/api/presence", headers=_auth(client._tok))).json()
    (status,) = [r["status"] for r in rows if r["username"] == "admin"]
    assert status["emoji"] == "🎧" and status["text"] == "Focus"
    assert status["expires_at"] is not None


async def test_list_presence_hides_expired_status(client):
    await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "latitude": 1.0,
            "longitude": 2.0,
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    await client._db.enqueue(
        "UPDATE users SET status_text='old', "
        "status_expires_at='2000-01-01T00:00:00+00:00' WHERE username='admin'"
    )
    rows = await (await client.get("/api/presence", headers=_auth(client._tok))).json()
    assert [r["status"] for r in rows if r["username"] == "admin"] == [None]

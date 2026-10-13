"""Tests for GET /api/spaces/{id}/presence (§23.80 / §L3).

Scenarios covered:
* feature_location off → empty list + feature_enabled=False
* feature_location on, member opted in → GPS surfaces (no zone_name)
* feature_location on, member NOT opted in → entry filtered out
* non-member → 403

Per §23.8.6, the presence response carries GPS only — zones are stripped at
the household boundary so HA-defined zone names never reach a space-bound
payload. Per-space display zones (§23.8.7) are matched client-side.
"""

from __future__ import annotations

from .conftest import _auth


async def _create_space(
    client,
    *,
    space_type="household",
    join_mode="open",
    lat=None,
    lon=None,
    radius_km=None,
):
    body = {"name": "Location Test", "space_type": space_type, "join_mode": join_mode}
    if lat is not None:
        body["lat"] = lat
        body["lon"] = lon
        if radius_km is not None:
            body["radius_km"] = radius_km
    resp = await client.post("/api/spaces", json=body, headers=_auth(client._tok))
    assert resp.status == 201, await resp.text()
    return (await resp.json())["id"]


async def _seed_presence(client, *, username, user_id, lat, lon, accuracy=15.0):
    del user_id
    await client._db.enqueue(
        """
        INSERT INTO presence(
            username, entity_id, state, zone_name,
            latitude, longitude, gps_accuracy_m
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (username, username, "home", "home", lat, lon, accuracy),
    )


async def _enable_feature_location(client, space_id, *, enabled):
    await client._db.enqueue(
        "UPDATE spaces SET feature_location=? WHERE id=?",
        (1 if enabled else 0, space_id),
    )


async def _set_member_opt_in(client, space_id, user_id, *, enabled):
    await client._db.enqueue(
        "UPDATE space_members SET location_share_enabled=? "
        "WHERE space_id=? AND user_id=?",
        (1 if enabled else 0, space_id, user_id),
    )


async def test_non_member_gets_403(client):
    space_id = await _create_space(client)
    # drop ourselves from membership to simulate a non-member caller
    await client._db.enqueue(
        "DELETE FROM space_members WHERE space_id=?",
        (space_id,),
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_feature_disabled_returns_empty(client):
    space_id = await _create_space(client)
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["feature_enabled"] is False
    assert "location_mode" not in body
    assert body["entries"] == []


async def test_gps_returned_when_enabled_and_opted_in(client):
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    await _seed_presence(
        client,
        username="admin",
        user_id=client._uid,
        lat=47.3769,
        lon=8.5417,
        accuracy=12.0,
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["feature_enabled"] is True
    assert body["location_mode"] == "gps"
    assert len(body["entries"]) == 1
    e = body["entries"][0]
    assert e["latitude"] == 47.3769
    assert e["longitude"] == 8.5417
    assert e["gps_accuracy_m"] == 12.0
    # zone_name is NEVER on a space-bound payload.
    assert "zone_name" not in e


async def test_member_without_opt_in_filtered(client):
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    # caller is a member but has not opted in.
    await _set_member_opt_in(client, space_id, client._uid, enabled=False)
    await _seed_presence(
        client,
        username="admin",
        user_id=client._uid,
        lat=47.3769,
        lon=8.5417,
        accuracy=12.0,
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["feature_enabled"] is True
    assert body["entries"] == []


async def test_only_space_members_surface(client):
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    await _seed_presence(
        client,
        username="admin",
        user_id=client._uid,
        lat=47.1,
        lon=8.0,
    )
    # Non-member user's presence — should NOT surface.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("stranger", "outsider_uid", "Stranger"),
    )
    await _seed_presence(
        client,
        username="stranger",
        user_id="outsider_uid",
        lat=47.5,
        lon=8.5,
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    body = await r.json()
    user_ids = {e["user_id"] for e in body["entries"]}
    assert client._uid in user_ids
    assert "outsider_uid" not in user_ids


async def _set_location_mode(client, space_id, mode: str) -> None:
    await client._db.enqueue(
        "UPDATE spaces SET location_mode=? WHERE id=?",
        (mode, space_id),
    )


async def _seed_zone(client, space_id, *, zid, name, lat, lon, radius_m=200):
    await client._db.enqueue(
        """INSERT INTO space_zones(
            id, space_id, name, latitude, longitude, radius_m,
            color, created_by, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            zid,
            space_id,
            name,
            lat,
            lon,
            radius_m,
            "#3b82f6",
            client._uid,
            "2026-04-28T00:00:00+00:00",
            "2026-04-28T00:00:00+00:00",
        ),
    )


async def test_zone_only_mode_returns_zone_labels_no_gps(client):
    """`/api/spaces/{id}/presence` in zone_only mode returns each
    member's matched zone label and NO raw coordinates. Members
    outside every zone are dropped from the response."""
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    await _set_location_mode(client, space_id, "zone_only")
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    await _seed_zone(
        client,
        space_id,
        zid="z_office",
        name="Office",
        lat=47.3769,
        lon=8.5417,
    )
    await _seed_presence(
        client,
        username="admin",
        user_id=client._uid,
        lat=47.3769,
        lon=8.5417,
        accuracy=12.0,
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert r.status == 200
    assert body["feature_enabled"] is True
    assert body["location_mode"] == "zone_only"
    [e] = body["entries"]
    assert e["zone_id"] == "z_office"
    assert e["zone_name"] == "Office"
    assert "latitude" not in e
    assert "longitude" not in e
    assert "gps_accuracy_m" not in e


async def test_zone_only_mode_skips_members_outside_every_zone(client):
    """A zone_only space drops members whose GPS is outside every
    space-defined zone — silent skip per §23.8.6."""
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    await _set_location_mode(client, space_id, "zone_only")
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    await _seed_zone(
        client,
        space_id,
        zid="z_far",
        name="Faraway",
        lat=0.0,
        lon=0.0,
        radius_m=100,
    )
    await _seed_presence(
        client,
        username="admin",
        user_id=client._uid,
        lat=47.3769,
        lon=8.5417,
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["location_mode"] == "zone_only"
    assert body["entries"] == []


# ─── Remote member pins (§D1b cross-household location sharing) ────────


async def _seed_remote_member(
    client,
    space_id,
    *,
    instance_id,
    user_id,
    display_name=None,
):
    """Seat a §D1b remote member + a remote_instance row to satisfy
    FK constraints on remote_users (used by display-name freshness)."""
    await client._db.enqueue(
        "INSERT OR IGNORE INTO remote_instances"
        "(id, display_name, remote_identity_pk, key_self_to_remote,"
        " key_remote_to_self, remote_inbox_url, local_inbox_id, status,"
        " source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            "Peer",
            "00" * 32,
            "k1",
            "k2",
            "https://peer/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
        ),
    )
    await client._db.enqueue(
        "INSERT INTO space_remote_members"
        "(space_id, instance_id, user_id, display_name)"
        " VALUES(?,?,?,?)",
        (space_id, instance_id, user_id, display_name),
    )


async def _seed_remote_pin(
    client,
    space_id,
    *,
    instance_id,
    user_id,
    lat,
    lon,
):
    """Insert a row as if SPACE_LOCATION_UPDATED had landed."""
    await client._db.enqueue(
        "INSERT INTO space_remote_member_locations"
        "(space_id, instance_id, user_id, mode, latitude, longitude,"
        " accuracy_m) VALUES(?,?,?,?,?,?,?)",
        (space_id, instance_id, user_id, "gps", lat, lon, 12.0),
    )


async def test_presence_includes_remote_member_pins(client):
    """Cross-household location sharing: a remote member who opted
    in on their own household and federated a SPACE_LOCATION_UPDATED
    must surface on the space map. Pascal's repro: "I can't see her on
    the space map and the map only says 1 user sharing location"."""
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    await _seed_presence(
        client,
        username="admin",
        user_id=client._uid,
        lat=52.5200,
        lon=13.4050,
    )
    await _seed_remote_member(
        client,
        space_id,
        instance_id="peer-jacqueline",
        user_id="uid-jacqueline",
        display_name="Jacqueline",
    )
    await _seed_remote_pin(
        client,
        space_id,
        instance_id="peer-jacqueline",
        user_id="uid-jacqueline",
        lat=48.1351,
        lon=11.5820,
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert r.status == 200
    assert body["location_mode"] == "gps"
    assert len(body["entries"]) == 2
    jacqueline = next(e for e in body["entries"] if e["user_id"] == "uid-jacqueline")
    assert jacqueline["display_name"] == "Jacqueline"
    assert jacqueline["latitude"] == 48.1351
    assert jacqueline["longitude"] == 11.5820
    assert jacqueline["instance_id"] == "peer-jacqueline"


async def test_presence_includes_remote_member_picture_url(client):
    """The map renders one pin per entry, using ``picture_url`` for the
    avatar. Pascal's report: own avatar shows but cross-household
    members are initials. Root cause: ``_remote_member_pin_entries``
    used to hardcode ``picture_url: None``. With the fix, the route
    looks up ``remote_users.picture_hash`` (kept fresh by
    ``USERS_SYNC``) and builds the same
    ``api/users/{user_id}/picture?v=<hash>`` URL the member list uses,
    then signs it through the media signer."""
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    await _seed_remote_member(
        client,
        space_id,
        instance_id="peer-jacqueline",
        user_id="uid-jacqueline",
        display_name="Jacqueline",
    )
    await client._db.enqueue(
        "INSERT INTO remote_users"
        "(user_id, instance_id, remote_username, display_name,"
        " picture_hash, synced_at, deprovisioned_at)"
        " VALUES(?,?,?,?,?, datetime('now'), NULL)",
        (
            "uid-jacqueline",
            "peer-jacqueline",
            "jacqueline",
            "Jacqueline",
            "feedface",
        ),
    )
    await _seed_remote_pin(
        client,
        space_id,
        instance_id="peer-jacqueline",
        user_id="uid-jacqueline",
        lat=48.1351,
        lon=11.5820,
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    body = await r.json()
    jacqueline = next(e for e in body["entries"] if e["user_id"] == "uid-jacqueline")
    assert jacqueline["picture_url"] is not None
    assert "api/users/uid-jacqueline/picture" in jacqueline["picture_url"]
    # The signer ran — the URL carries the cache-busting hash and
    # the signature query the SPA needs for <img> loads under ingress.
    assert "v=feedface" in jacqueline["picture_url"]
    # Signed exactly once — a second pass appended a second exp/sig pair.
    assert jacqueline["picture_url"].count("sig=") == 1
    assert jacqueline["picture_url"].count("exp=") == 1


async def test_presence_skips_remote_pin_without_member_row(client):
    """If the SPACE_REMOTE_MEMBER_REMOVED cleanup raced ahead but
    a stale location row survived, we must NOT render a ghost pin."""
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    # No membership row — only a stranded location row.
    await _seed_remote_pin(
        client,
        space_id,
        instance_id="peer-stale",
        user_id="uid-ghost",
        lat=52.5,
        lon=13.4,
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    body = await r.json()
    # Admin is opted out by default — no local entries. Stale remote
    # pin is dropped.
    assert body["entries"] == []


async def test_presence_reports_total_members_and_sharing_count(client):
    """Pascal's repro: space has 3 members (2 local + 1 remote) but
    the map header showed '2 of 2'. The denominator must be the full
    roster (local + remote member rows), the numerator the count of
    members who opted in to share with this space."""
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    # Local member 1: admin (the caller) — opted in, has GPS.
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    await _seed_presence(
        client,
        username="admin",
        user_id=client._uid,
        lat=52.5,
        lon=13.4,
    )
    # Local member 2: housemate — opted in, no GPS yet. They count
    # toward total_members AND toward sharing_count (intent), even
    # without an active pin.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('housemate', 'uid-housemate', 'Housemate', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role, joined_at, "
        "location_share_enabled) VALUES(?, 'uid-housemate', 'member', "
        "datetime('now'), 1)",
        (space_id,),
    )
    # Remote member: opted in via their own household, federated a
    # SPACE_LOCATION_UPDATED to us, has a current pin row.
    await _seed_remote_member(
        client,
        space_id,
        instance_id="peer-jacqueline",
        user_id="uid-jacqueline",
        display_name="Jacqueline",
    )
    await _seed_remote_pin(
        client,
        space_id,
        instance_id="peer-jacqueline",
        user_id="uid-jacqueline",
        lat=48.1,
        lon=11.5,
    )

    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert r.status == 200
    # 2 local members + 1 remote = 3 in the roster.
    assert body["total_members"] == 3
    # All 3 are sharing (admin via GPS row, housemate via opt-in
    # without GPS yet, Jacqueline via pin row existence).
    assert body["sharing_count"] == 3


async def test_presence_total_members_excludes_share_state(client):
    """A non-sharing member still counts toward total_members so the
    SPA's denominator is honest about the space size."""
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    # Admin opted-in.
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    # Housemate — present in the space but NOT sharing.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('housemate', 'uid-housemate', 'Housemate', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role, joined_at, "
        "location_share_enabled) VALUES(?, 'uid-housemate', 'member', "
        "datetime('now'), 0)",
        (space_id,),
    )
    r = await client.get(
        f"/api/spaces/{space_id}/presence",
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["total_members"] == 2
    assert body["sharing_count"] == 1


async def _seed_local_picture(client, picture_hash: str) -> None:
    await client._db.enqueue(
        "UPDATE users SET picture_hash=? WHERE username=?",
        (picture_hash, "admin"),
    )


async def test_gps_entries_carry_a_signed_picture_url(client):
    """The map pin loads ``picture_url`` straight into ``<img src>``; the
    picture route answers 401 to an unsigned URL, so it must be signed."""
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    await _seed_local_picture(client, "cafe01")
    await _seed_presence(
        client, username="admin", user_id=client._uid, lat=47.3769, lon=8.5417
    )
    r = await client.get(f"/api/spaces/{space_id}/presence", headers=_auth(client._tok))
    [e] = (await r.json())["entries"]
    assert e["picture_url"].startswith(f"api/users/{client._uid}/picture?v=cafe01")
    assert "sig=" in e["picture_url"] and "exp=" in e["picture_url"]


async def test_zone_only_entries_carry_a_signed_picture_url(client):
    space_id = await _create_space(client)
    await _enable_feature_location(client, space_id, enabled=True)
    await _set_location_mode(client, space_id, "zone_only")
    await _set_member_opt_in(client, space_id, client._uid, enabled=True)
    await _seed_local_picture(client, "cafe02")
    await _seed_zone(
        client, space_id, zid="z_home", name="Home", lat=47.3769, lon=8.5417
    )
    await _seed_presence(
        client, username="admin", user_id=client._uid, lat=47.3769, lon=8.5417
    )
    r = await client.get(f"/api/spaces/{space_id}/presence", headers=_auth(client._tok))
    [e] = (await r.json())["entries"]
    assert e["picture_url"].startswith(f"api/users/{client._uid}/picture?v=cafe02")
    assert "sig=" in e["picture_url"]

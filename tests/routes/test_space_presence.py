"""Tests for GET /api/spaces/{id}/presence (§23.80 / §L3).

Scenarios covered:
* feature_location off → empty list + feature_enabled=False
* feature_location on, mode=gps → members with coordinates surface
* feature_location on, mode=zone_only → coordinates stripped
* non-member → 403
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
    # The presence row's user_id comes from the joined users table at
    # read time — seed presence keyed by username only.
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


async def _set_location_mode(client, space_id, enabled, mode):
    """Direct DB flip — PATCH features via API uses a different
    feature-name mapping; fastest path for the test is to update
    the columns directly.
    """
    await client._db.enqueue(
        "UPDATE spaces SET feature_location=?, location_mode=? WHERE id=?",
        (1 if enabled else 0, mode, space_id),
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
    assert body["location_mode"] == "off"
    assert body["entries"] == []


async def test_gps_mode_returns_coordinates(client):
    space_id = await _create_space(client)
    await _set_location_mode(client, space_id, enabled=True, mode="gps")
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


async def test_zone_only_strips_coordinates(client):
    space_id = await _create_space(client)
    await _set_location_mode(
        client,
        space_id,
        enabled=True,
        mode="zone_only",
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
    assert r.status == 200
    body = await r.json()
    assert body["location_mode"] == "zone_only"
    e = body["entries"][0]
    assert e["latitude"] is None
    assert e["longitude"] is None
    assert e["gps_accuracy_m"] is None
    # Zone + state must still be present.
    assert e["zone_name"] == "home"
    assert e["state"] == "home"


async def test_only_space_members_surface(client):
    space_id = await _create_space(client)
    await _set_location_mode(client, space_id, enabled=True, mode="gps")
    # Caller (admin) is a member. Seed caller's presence.
    await _seed_presence(
        client,
        username="admin",
        user_id=client._uid,
        lat=47.1,
        lon=8.0,
    )
    # Seed a non-member user's presence — should NOT surface.
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

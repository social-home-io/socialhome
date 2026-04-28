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

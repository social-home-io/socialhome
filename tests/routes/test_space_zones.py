"""Tests for ``/api/spaces/{id}/zones`` (§23.8.7) +
``/api/spaces/{id}/members/me/location-sharing`` (§23.8.8).

These exercise the full HTTP route → service → repo path through
``aiohttp_client`` so the central exception map, auth, and JSON
shape all stay covered.
"""

from __future__ import annotations

from .conftest import _auth


async def _create_space(client, *, name: str = "Family") -> str:
    r = await client.post(
        "/api/spaces",
        json={"name": name, "space_type": "household", "join_mode": "open"},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    return (await r.json())["id"]


async def _enable_location(client, space_id: str) -> None:
    await client._db.enqueue(
        "UPDATE spaces SET feature_location=1 WHERE id=?",
        (space_id,),
    )


# ─── GET / POST collection ──────────────────────────────────────────────


async def test_list_zones_empty(client):
    space_id = await _create_space(client)
    r = await client.get(
        f"/api/spaces/{space_id}/zones",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"zones": []}


async def test_create_zone_returns_201(client):
    space_id = await _create_space(client)
    r = await client.post(
        f"/api/spaces/{space_id}/zones",
        json={
            "name": "Office",
            "latitude": 47.3769,
            "longitude": 8.5417,
            "radius_m": 150,
            "color": "#3b82f6",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["name"] == "Office"
    assert body["latitude"] == 47.3769
    assert body["longitude"] == 8.5417
    assert body["radius_m"] == 150
    assert body["color"] == "#3b82f6"
    assert body["id"].startswith("z_")


async def test_create_zone_invalid_radius_422(client):
    space_id = await _create_space(client)
    r = await client.post(
        f"/api/spaces/{space_id}/zones",
        json={
            "name": "Tiny",
            "latitude": 47.0,
            "longitude": 8.0,
            "radius_m": 5,  # below min 25
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_create_zone_duplicate_name_409(client):
    space_id = await _create_space(client)
    body = {
        "name": "Office",
        "latitude": 47.0,
        "longitude": 8.0,
        "radius_m": 200,
    }
    r1 = await client.post(
        f"/api/spaces/{space_id}/zones",
        json=body,
        headers=_auth(client._tok),
    )
    assert r1.status == 201
    r2 = await client.post(
        f"/api/spaces/{space_id}/zones",
        json=body,
        headers=_auth(client._tok),
    )
    assert r2.status == 409


async def test_create_zone_non_admin_403(client):
    space_id = await _create_space(client)
    # Drop ourselves to a regular member so admin-gate fires.
    await client._db.enqueue(
        "UPDATE space_members SET role='member' WHERE space_id=?",
        (space_id,),
    )
    r = await client.post(
        f"/api/spaces/{space_id}/zones",
        json={
            "name": "Office",
            "latitude": 47.0,
            "longitude": 8.0,
            "radius_m": 200,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ─── PATCH / DELETE detail ──────────────────────────────────────────────


async def _seed_zone(client, space_id: str, **over) -> str:
    r = await client.post(
        f"/api/spaces/{space_id}/zones",
        json={
            "name": over.get("name", "Office"),
            "latitude": over.get("latitude", 47.0),
            "longitude": over.get("longitude", 8.0),
            "radius_m": over.get("radius_m", 200),
            "color": over.get("color", "#3b82f6"),
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    return (await r.json())["id"]


async def test_patch_zone_partial_update(client):
    space_id = await _create_space(client)
    zid = await _seed_zone(client, space_id)
    r = await client.patch(
        f"/api/spaces/{space_id}/zones/{zid}",
        json={"radius_m": 400},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["radius_m"] == 400
    assert body["name"] == "Office"  # untouched
    assert body["color"] == "#3b82f6"  # untouched


async def test_patch_zone_clear_color(client):
    space_id = await _create_space(client)
    zid = await _seed_zone(client, space_id)
    r = await client.patch(
        f"/api/spaces/{space_id}/zones/{zid}",
        json={"color": None},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["color"] is None


async def test_patch_zone_unknown_id_404(client):
    space_id = await _create_space(client)
    r = await client.patch(
        f"/api/spaces/{space_id}/zones/z_nope",
        json={"radius_m": 400},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_delete_zone_returns_204(client):
    space_id = await _create_space(client)
    zid = await _seed_zone(client, space_id)
    r = await client.delete(
        f"/api/spaces/{space_id}/zones/{zid}",
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_delete_zone_unknown_id_404(client):
    space_id = await _create_space(client)
    r = await client.delete(
        f"/api/spaces/{space_id}/zones/z_nope",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_delete_zone_non_admin_403(client):
    space_id = await _create_space(client)
    zid = await _seed_zone(client, space_id)
    await client._db.enqueue(
        "UPDATE space_members SET role='member' WHERE space_id=?",
        (space_id,),
    )
    r = await client.delete(
        f"/api/spaces/{space_id}/zones/{zid}",
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ─── Member self-service location-sharing toggle (§23.8.8) ──────────────


async def test_member_location_sharing_patch_enabled(client):
    space_id = await _create_space(client)
    await _enable_location(client, space_id)
    r = await client.patch(
        f"/api/spaces/{space_id}/members/me/location-sharing",
        json={"enabled": True},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["location_share_enabled"] is True
    # And the row is actually flipped.
    row = await client._db.fetchone(
        "SELECT location_share_enabled FROM space_members"
        " WHERE space_id=? AND user_id=?",
        (space_id, client._uid),
    )
    assert row["location_share_enabled"] == 1


async def test_member_location_sharing_patch_disable(client):
    space_id = await _create_space(client)
    await _enable_location(client, space_id)
    # Start enabled.
    await client.patch(
        f"/api/spaces/{space_id}/members/me/location-sharing",
        json={"enabled": True},
        headers=_auth(client._tok),
    )
    r = await client.patch(
        f"/api/spaces/{space_id}/members/me/location-sharing",
        json={"enabled": False},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["location_share_enabled"] is False


async def test_member_location_sharing_invalid_body_422(client):
    space_id = await _create_space(client)
    r = await client.patch(
        f"/api/spaces/{space_id}/members/me/location-sharing",
        json={"enabled": "yes"},  # not a bool
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_zones_unauthenticated_get_401(client):
    space_id = await _create_space(client)
    r = await client.get(f"/api/spaces/{space_id}/zones")  # no auth header
    assert r.status == 401


async def test_zones_unauthenticated_post_401(client):
    space_id = await _create_space(client)
    r = await client.post(
        f"/api/spaces/{space_id}/zones",
        json={"name": "X", "latitude": 0, "longitude": 0, "radius_m": 200},
    )
    assert r.status == 401


async def test_zones_unauthenticated_patch_401(client):
    space_id = await _create_space(client)
    r = await client.patch(
        f"/api/spaces/{space_id}/zones/z_x",
        json={"radius_m": 400},
    )
    assert r.status == 401


async def test_zones_unauthenticated_delete_401(client):
    space_id = await _create_space(client)
    r = await client.delete(f"/api/spaces/{space_id}/zones/z_x")
    assert r.status == 401


async def test_patch_zone_full_field_set(client):
    """Hits every field branch in the PATCH handler — name, latitude,
    longitude, radius_m, color all in one request."""
    space_id = await _create_space(client)
    zid = await _seed_zone(client, space_id)
    r = await client.patch(
        f"/api/spaces/{space_id}/zones/{zid}",
        json={
            "name": "Renamed",
            "latitude": 48.0,
            "longitude": 9.0,
            "radius_m": 300,
            "color": "#10b981",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["name"] == "Renamed"
    assert body["latitude"] == 48.0
    assert body["longitude"] == 9.0
    assert body["radius_m"] == 300
    assert body["color"] == "#10b981"


async def test_member_location_sharing_non_member_404(client):
    space_id = await _create_space(client)
    # Drop ourselves from the members table.
    await client._db.enqueue(
        "DELETE FROM space_members WHERE space_id=?",
        (space_id,),
    )
    r = await client.patch(
        f"/api/spaces/{space_id}/members/me/location-sharing",
        json={"enabled": True},
        headers=_auth(client._tok),
    )
    assert r.status == 404

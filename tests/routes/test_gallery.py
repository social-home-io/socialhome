"""HTTP tests for gallery routes."""

from __future__ import annotations


from .conftest import _auth


async def _make_space(client, *, sid: str = "sp-1") -> None:
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'X', 'inst', 'admin', ?)",
        (sid, "ab" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'owner')",
        (sid, client._uid),
    )


# ─── Authentication ──────────────────────────────────────────────────────


async def test_list_household_albums_requires_auth(client):
    r = await client.get("/api/gallery/albums")
    assert r.status == 401


async def test_list_space_albums_requires_auth(client):
    r = await client.get("/api/spaces/sp-1/gallery/albums")
    assert r.status == 401


async def test_create_album_requires_auth(client):
    r = await client.post("/api/gallery/albums", json={"name": "X"})
    assert r.status == 401


# ─── Household album CRUD ───────────────────────────────────────────────


async def test_household_albums_initially_empty(client):
    r = await client.get("/api/gallery/albums", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json()) == []


async def test_create_household_album(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Personal photos"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["name"] == "Personal photos"
    assert body["space_id"] is None


async def test_create_album_empty_name_422(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_create_album_bad_json_400(client):
    r = await client.post(
        "/api/gallery/albums",
        data="bad",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


# ─── Space album CRUD ───────────────────────────────────────────────────


async def test_create_space_album(client):
    await _make_space(client)
    r = await client.post(
        "/api/spaces/sp-1/gallery/albums",
        json={"name": "Trip 2026", "description": "A trip"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["space_id"] == "sp-1"


async def test_list_space_albums_non_member_403(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-other', 'X', 'inst', 'someone', ?)",
        ("ab" * 32,),
    )
    r = await client.get(
        "/api/spaces/sp-other/gallery/albums",
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ─── Get / update / delete album ────────────────────────────────────────


async def test_get_album_unknown_404(client):
    r = await client.get(
        "/api/gallery/albums/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_get_album_existing(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "X"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.get(
        f"/api/gallery/albums/{aid}",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["id"] == aid


async def test_update_album_renames(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Original"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.patch(
        f"/api/gallery/albums/{aid}",
        json={"name": "Renamed"},
        headers=_auth(client._tok),
    )
    assert r.status == 204
    r = await client.get(
        f"/api/gallery/albums/{aid}",
        headers=_auth(client._tok),
    )
    assert (await r.json())["name"] == "Renamed"


async def test_update_album_bad_json_400(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "X"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.patch(
        f"/api/gallery/albums/{aid}",
        data="bad",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_delete_album_returns_204(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Bye"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.delete(
        f"/api/gallery/albums/{aid}",
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_set_retention_exempt(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Keep me"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.post(
        f"/api/gallery/albums/{aid}/retention",
        json={"retention_exempt": True},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["retention_exempt"] is True


# ─── Items ──────────────────────────────────────────────────────────────


async def test_list_items_unknown_album_404(client):
    r = await client.get(
        "/api/gallery/albums/missing/items",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_list_items_empty_album_returns_array(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Empty"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.get(
        f"/api/gallery/albums/{aid}/items",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json()) == []


async def test_delete_item_unknown_204(client):
    r = await client.delete(
        "/api/gallery/items/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 204

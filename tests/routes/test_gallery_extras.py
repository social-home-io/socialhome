"""Extra coverage for routes/gallery.py — auth branches + multipart path."""

from __future__ import annotations


from .conftest import _auth


# ─── Unauth ──────────────────────────────────────────────────────────────


async def test_get_album_unauth_401(client):
    r = await client.get("/api/gallery/albums/x")
    assert r.status == 401


async def test_update_album_unauth_401(client):
    r = await client.patch("/api/gallery/albums/x", json={})
    assert r.status == 401


async def test_delete_album_unauth_401(client):
    r = await client.delete("/api/gallery/albums/x")
    assert r.status == 401


async def test_set_retention_unauth_401(client):
    r = await client.post("/api/gallery/albums/x/retention", json={})
    assert r.status == 401


async def test_list_items_unauth_401(client):
    r = await client.get("/api/gallery/albums/x/items")
    assert r.status == 401


async def test_upload_item_unauth_401(client):
    r = await client.post("/api/gallery/albums/x/items", data=b"")
    assert r.status == 401


async def test_delete_item_unauth_401(client):
    r = await client.delete("/api/gallery/items/x")
    assert r.status == 401


async def test_create_space_album_unauth_401(client):
    r = await client.post("/api/spaces/sp-1/gallery/albums", json={"name": "X"})
    assert r.status == 401


# ─── Edge cases ─────────────────────────────────────────────────────────


async def test_create_space_album_non_member_403(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-x', 'X', 'inst', 'somebody', ?)",
        ("ab" * 32,),
    )
    r = await client.post(
        "/api/spaces/sp-x/gallery/albums",
        json={"name": "Hijack"},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_create_album_too_long_name_422(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "x" * 200},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_update_album_unknown_404(client):
    r = await client.patch(
        "/api/gallery/albums/missing",
        json={"name": "Renamed"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_set_retention_unknown_404(client):
    r = await client.post(
        "/api/gallery/albums/missing/retention",
        json={"retention_exempt": True},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_set_retention_no_body_treated_as_false(client):
    """POST without ``retention_exempt`` defaults to False — no crash."""
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "X"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.post(
        f"/api/gallery/albums/{aid}/retention",
        data="not-json",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    # Bad JSON → still treated as empty dict → retention_exempt=false.
    assert r.status == 200
    assert (await r.json())["retention_exempt"] is False


async def test_upload_item_no_body_422_or_404(client):
    """Empty multipart → 422 (missing file) or 404 (album missing)."""
    r = await client.post(
        "/api/gallery/albums/missing/items",
        data=b"",
        headers={**_auth(client._tok), "Content-Type": "image/jpeg"},
    )
    assert r.status in (404, 422)


async def test_list_items_pagination_query_params(client):
    """`limit` + `before` query params parse cleanly."""
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "X"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.get(
        f"/api/gallery/albums/{aid}/items?limit=10&before=2099-01-01",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    r = await client.get(
        f"/api/gallery/albums/{aid}/items?limit=not-a-number",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_list_albums_invalid_limit_falls_back(client):
    r = await client.get(
        "/api/gallery/albums?limit=not-a-number",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_list_space_albums_invalid_limit_falls_back(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-y', 'Y', 'inst', 'admin', ?)",
        ("ab" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-y', ?, 'owner')",
        (client._uid,),
    )
    r = await client.get(
        "/api/spaces/sp-y/gallery/albums?limit=garbage",
        headers=_auth(client._tok),
    )
    assert r.status == 200

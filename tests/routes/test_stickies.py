"""Tests for social_home.routes.stickies."""

from .conftest import _auth


async def test_create_sticky(client):
    """POST /api/stickies creates a sticky note."""
    r = await client.post(
        "/api/stickies", json={"content": "Remember this"}, headers=_auth(client._tok)
    )
    assert r.status == 201


async def test_list_stickies(client):
    """GET /api/stickies returns household stickies."""
    await client.post(
        "/api/stickies", json={"content": "Note"}, headers=_auth(client._tok)
    )
    r = await client.get("/api/stickies", headers=_auth(client._tok))
    assert r.status == 200
    assert len(await r.json()) >= 1


async def test_update_sticky(client):
    """PATCH /api/stickies/{id} updates content."""
    r = await client.post(
        "/api/stickies", json={"content": "v1"}, headers=_auth(client._tok)
    )
    sid = (await r.json())["id"]
    r2 = await client.patch(
        f"/api/stickies/{sid}", json={"content": "v2"}, headers=_auth(client._tok)
    )
    assert r2.status == 200


async def test_delete_sticky(client):
    """DELETE /api/stickies/{id} removes it."""
    r = await client.post(
        "/api/stickies", json={"content": "tmp"}, headers=_auth(client._tok)
    )
    sid = (await r.json())["id"]
    r2 = await client.delete(f"/api/stickies/{sid}", headers=_auth(client._tok))
    assert r2.status in (200, 204)

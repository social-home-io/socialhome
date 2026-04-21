"""Tests for social_home.routes.shopping."""

from .conftest import _auth


async def test_shopping_add_item(client):
    """POST /api/shopping creates an item."""
    r = await client.post(
        "/api/shopping", json={"text": "Milk"}, headers=_auth(client._tok)
    )
    assert r.status == 201


async def test_shopping_list_items(client):
    """GET /api/shopping returns the list."""
    await client.post(
        "/api/shopping", json={"text": "Bread"}, headers=_auth(client._tok)
    )
    r = await client.get("/api/shopping", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert len(body) >= 1


async def test_shopping_complete(client):
    """PATCH /api/shopping/{id}/complete marks an item done."""
    r = await client.post(
        "/api/shopping", json={"text": "Eggs"}, headers=_auth(client._tok)
    )
    item = await r.json()
    r2 = await client.patch(
        f"/api/shopping/{item['id']}/complete", headers=_auth(client._tok)
    )
    assert r2.status == 200


async def test_shopping_empty_text_rejected(client):
    """POST /api/shopping with empty text returns 422."""
    r = await client.post(
        "/api/shopping", json={"text": "  "}, headers=_auth(client._tok)
    )
    assert r.status == 422

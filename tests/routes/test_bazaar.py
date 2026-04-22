"""Tests for socialhome.routes.bazaar."""

from .conftest import _auth


async def test_list_active_listings(client):
    """GET /api/bazaar returns active listings (empty at start)."""
    r = await client.get("/api/bazaar", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert isinstance(body, list)

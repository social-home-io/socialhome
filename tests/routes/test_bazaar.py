"""Tests for socialhome.routes.bazaar."""

from .conftest import _auth


async def test_list_active_listings(client):
    """GET /api/bazaar returns active listings (empty at start)."""
    r = await client.get("/api/bazaar", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert isinstance(body, list)


async def _make_space(client, **extra) -> str:
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces", json={"name": "Market", "emoji": "🛍", **extra}, headers=h
    )
    assert r.status == 201
    sid = (await r.json())["id"]
    # A new space starts without the Bazaar (owner decision 3): turn it on.
    r = await client.patch(
        f"/api/spaces/{sid}", json={"features": {"bazaar": True}}, headers=h
    )
    assert r.status == 200, await r.text()
    return sid


async def _make_listing(client, sid: str, **extra) -> dict:
    h = _auth(client._tok)
    r = await client.post(
        "/api/bazaar",
        json={
            "space_id": sid,
            "title": "Bike",
            "mode": "fixed",
            "currency": "EUR",
            "price": 5000,
            **extra,
        },
        headers=h,
    )
    assert r.status == 201, await r.text()
    return await r.json()


async def test_space_bazaar_tab_lists_listings(client):
    """GET /api/spaces/{id}/bazaar returns that space's listings."""
    h = _auth(client._tok)
    sid = await _make_space(client)
    await _make_listing(client, sid)
    r = await client.get(f"/api/spaces/{sid}/bazaar", headers=h)
    assert r.status == 200
    body = await r.json()
    assert len(body) == 1
    assert body[0]["title"] == "Bike"
    assert body[0]["space_id"] == sid


async def test_listing_hidden_from_feed_by_default(client):
    """A new listing lives in the Bazaar tab, not the feed (announce off)."""
    h = _auth(client._tok)
    sid = await _make_space(client)
    await _make_listing(client, sid)
    feed = await (await client.get(f"/api/spaces/{sid}/feed", headers=h)).json()
    assert all(p["type"] != "bazaar" for p in feed), feed
    # ...but the listing is in the Bazaar tab.
    tab = await (await client.get(f"/api/spaces/{sid}/bazaar", headers=h)).json()
    assert len(tab) == 1


async def test_listing_appears_in_feed_when_announced(client):
    """``announce_in_feed=True`` surfaces the wrapper post in the feed."""
    h = _auth(client._tok)
    sid = await _make_space(client)
    await _make_listing(client, sid, announce_in_feed=True)
    feed = await (await client.get(f"/api/spaces/{sid}/feed", headers=h)).json()
    assert any(p["type"] == "bazaar" for p in feed), feed


async def test_bazaar_feature_off_blocks_create_and_browse(client):
    """With the space's bazaar feature disabled, creating a listing is
    rejected and the space bazaar endpoint 403s."""
    h = _auth(client._tok)
    sid = await _make_space(client)
    patch = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"bazaar": False, "pages": True, "gallery": True}},
        headers=h,
    )
    assert patch.status == 200
    create = await client.post(
        "/api/bazaar",
        json={
            "space_id": sid,
            "title": "X",
            "mode": "fixed",
            "currency": "EUR",
            "price": 100,
        },
        headers=h,
    )
    assert create.status == 403
    browse = await client.get(f"/api/spaces/{sid}/bazaar", headers=h)
    assert browse.status == 403

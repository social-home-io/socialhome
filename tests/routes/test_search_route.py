"""HTTP tests for /api/search."""

from __future__ import annotations

from .conftest import _auth


async def test_search_requires_auth(client):
    r = await client.get("/api/search?q=hello")
    assert r.status == 401


async def test_search_empty_query_returns_no_hits(client):
    r = await client.get("/api/search?q=", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["hits"] == []


async def test_search_invalid_scope_returns_422(client):
    r = await client.get("/api/search?q=x&scope=evil", headers=_auth(client._tok))
    assert r.status == 422


async def test_search_after_post_create_returns_hit(client):
    # Create a post and search for one of its words.
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "the unmistakable phrase appears here"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.get(
        "/api/search?q=unmistakable",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    hits = (await r.json())["hits"]
    assert len(hits) >= 1
    assert "unmistakable" in hits[0]["snippet"].lower()


async def test_search_filters_by_scope(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "household-only-needle"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.get(
        "/api/search?q=household-only-needle&scope=post",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_search_invalid_limit_falls_back_to_default(client):
    r = await client.get(
        "/api/search?q=anything&limit=not-a-number",
        headers=_auth(client._tok),
    )
    assert r.status == 200

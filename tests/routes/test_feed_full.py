"""Full route coverage for feed endpoints — error branches."""

from .conftest import _auth


async def test_feed_list_with_before(client):
    """GET /api/feed with before= cursor works."""
    h = _auth(client._tok)
    await client.post(
        "/api/feed/posts", json={"type": "text", "content": "A"}, headers=h
    )
    await client.post(
        "/api/feed/posts", json={"type": "text", "content": "B"}, headers=h
    )
    r = await client.get("/api/feed", headers=h)
    posts = await r.json()
    if len(posts) >= 2:
        r2 = await client.get(f"/api/feed?before={posts[-1]['created_at']}", headers=h)
        assert r2.status == 200


async def test_feed_invalid_json(client):
    """POST with invalid JSON returns 400."""
    h = {**_auth(client._tok), "Content-Type": "application/json"}
    r = await client.post("/api/feed/posts", data="not json", headers=h)
    assert r.status == 400


async def test_feed_comment_list_empty(client):
    """GET comments on a new post returns empty list."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/feed/posts", json={"type": "text", "content": "X"}, headers=h
    )
    pid = (await r.json())["id"]
    r2 = await client.get(f"/api/feed/posts/{pid}/comments", headers=h)
    assert r2.status == 200
    assert await r2.json() == []

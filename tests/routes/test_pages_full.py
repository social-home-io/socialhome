"""Full route coverage for pages endpoints."""

from .conftest import _auth


async def test_page_full_lifecycle(client):
    """Create → get → update → lock → unlock → delete."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/pages", json={"title": "Wiki", "content": "Hello"}, headers=h
    )
    assert r.status == 201
    pid = (await r.json())["id"]

    r = await client.get(f"/api/pages/{pid}", headers=h)
    assert r.status == 200
    assert (await r.json())["title"] == "Wiki"

    r = await client.patch(f"/api/pages/{pid}", json={"content": "Updated"}, headers=h)
    assert r.status == 200

    r = await client.post(f"/api/pages/{pid}/lock", headers=h)
    assert r.status == 200

    r = await client.delete(f"/api/pages/{pid}/lock", headers=h)
    assert r.status == 200

    r = await client.delete(f"/api/pages/{pid}", headers=h)
    assert r.status in (200, 204)


async def test_page_list(client):
    """GET /api/pages returns list."""
    h = _auth(client._tok)
    await client.post("/api/pages", json={"title": "T", "content": "C"}, headers=h)
    r = await client.get("/api/pages", headers=h)
    assert r.status == 200
    assert len(await r.json()) >= 1

"""Full route coverage for space endpoints."""

from .conftest import _auth


async def test_space_full_lifecycle(client):
    """Create → get → update → members → feed → posts → dissolve."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces", json={"name": "TestSpace", "emoji": "🏠"}, headers=h
    )
    assert r.status == 201
    sid = (await r.json())["id"]

    r = await client.get(f"/api/spaces/{sid}", headers=h)
    assert r.status == 200
    assert (await r.json())["name"] == "TestSpace"

    r = await client.patch(f"/api/spaces/{sid}", json={"name": "Updated"}, headers=h)
    assert r.status == 200

    r = await client.get(f"/api/spaces/{sid}/members", headers=h)
    assert r.status == 200
    assert len(await r.json()) >= 1

    r = await client.get(f"/api/spaces/{sid}/feed", headers=h)
    assert r.status == 200

    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "hello space"},
        headers=h,
    )
    assert r.status == 201

    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens", json={"uses": 1}, headers=h
    )
    assert r.status == 201

    r = await client.post(
        f"/api/spaces/{sid}/ban", json={"user_id": "nonexistent"}, headers=h
    )
    # May return 404 if user doesn't exist or 200 — just ensure no 500
    assert r.status < 500

    r = await client.delete(f"/api/spaces/{sid}", headers=h)
    assert r.status == 200

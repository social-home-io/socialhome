"""Full route coverage for stickies endpoints."""

from .conftest import _auth


async def test_sticky_full_lifecycle(client):
    """Create → list → update content → update position/color → delete."""
    h = _auth(client._tok)
    r = await client.post("/api/stickies", json={"content": "Remember"}, headers=h)
    assert r.status == 201
    sid = (await r.json())["id"]

    r = await client.get("/api/stickies", headers=h)
    assert r.status == 200
    assert len(await r.json()) >= 1

    r = await client.patch(
        f"/api/stickies/{sid}",
        json={"content": "Updated", "color": "#FF0000"},
        headers=h,
    )
    assert r.status == 200

    r = await client.delete(f"/api/stickies/{sid}", headers=h)
    assert r.status in (200, 204)

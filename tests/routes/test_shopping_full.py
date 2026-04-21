"""Full route coverage for shopping endpoints."""

from .conftest import _auth


async def test_shopping_full_lifecycle(client):
    """Add → list → complete → uncomplete → clear → delete."""
    h = _auth(client._tok)
    r = await client.post("/api/shopping", json={"text": "Milk"}, headers=h)
    assert r.status == 201
    item = await r.json()

    r = await client.post("/api/shopping", json={"text": "Bread"}, headers=h)
    assert r.status == 201

    r = await client.get("/api/shopping", headers=h)
    assert r.status == 200
    assert len(await r.json()) >= 2

    r = await client.patch(f"/api/shopping/{item['id']}/complete", headers=h)
    assert r.status == 200

    r = await client.patch(f"/api/shopping/{item['id']}/uncomplete", headers=h)
    assert r.status == 200

    r = await client.patch(f"/api/shopping/{item['id']}/complete", headers=h)
    assert r.status == 200

    r = await client.post("/api/shopping/clear-completed", headers=h)
    assert r.status == 200

    # Remaining item
    items = await (await client.get("/api/shopping", headers=h)).json()
    for i in items:
        await client.delete(f"/api/shopping/{i['id']}", headers=h)

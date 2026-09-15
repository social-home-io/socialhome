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


async def test_shopping_store_rename_cascades_to_items(client):
    """PATCH /api/shopping/stores/{name} renames the catalogue row and
    every item that referenced it. Item ``store`` field gets bumped to
    the new name; the old name disappears from ``GET /stores``."""
    h = _auth(client._tok)
    r = await client.post(
        "/api/shopping",
        json={"text": "Milk", "store": "Aldi"},
        headers=h,
    )
    milk = await r.json()
    await client.post(
        "/api/shopping",
        json={"text": "Eggs", "store": "Aldi"},
        headers=h,
    )

    r = await client.patch(
        "/api/shopping/stores/Aldi",
        json={"name": "Coop"},
        headers=h,
    )
    assert r.status == 200
    assert (await r.json())["new_name"] == "Coop"

    stores = await (await client.get("/api/shopping/stores", headers=h)).json()
    names = [s["name"] for s in stores]
    assert "Aldi" not in names
    assert "Coop" in names

    items = await (await client.get("/api/shopping", headers=h)).json()
    for i in items:
        if i["id"] == milk["id"]:
            assert i["store"] == "Coop"


async def test_shopping_store_rename_missing_returns_404(client):
    h = _auth(client._tok)
    r = await client.patch(
        "/api/shopping/stores/Ghost",
        json={"name": "Coop"},
        headers=h,
    )
    assert r.status == 404


async def test_shopping_store_rename_collision_merges(client):
    """Renaming Aldi onto Migros while both exist folds Aldi's items
    into Migros rather than 409-ing — that is the household's only way
    to collapse a duplicate."""
    h = _auth(client._tok)
    await client.post(
        "/api/shopping",
        json={"text": "Milk", "store": "Aldi"},
        headers=h,
    )
    await client.post(
        "/api/shopping",
        json={"text": "Bread", "store": "Migros"},
        headers=h,
    )
    r = await client.patch(
        "/api/shopping/stores/Aldi",
        json={"name": "Migros"},
        headers=h,
    )
    assert r.status == 200
    body = await r.json()
    assert body["merged"] is True
    assert body["moved_items"] == 1


async def test_shopping_store_delete_clears_items_and_returns_count(client):
    """DELETE /api/shopping/stores/{name} clears every item that
    referenced it (``store`` set to NULL) and removes the catalogue
    row. Response carries the cleared-items count for the SPA toast."""
    h = _auth(client._tok)
    await client.post(
        "/api/shopping",
        json={"text": "Milk", "store": "Aldi"},
        headers=h,
    )
    await client.post(
        "/api/shopping",
        json={"text": "Eggs", "store": "Aldi"},
        headers=h,
    )

    r = await client.delete("/api/shopping/stores/Aldi", headers=h)
    assert r.status == 200
    assert (await r.json())["cleared"] == 2

    stores = await (await client.get("/api/shopping/stores", headers=h)).json()
    assert "Aldi" not in [s["name"] for s in stores]
    items = await (await client.get("/api/shopping", headers=h)).json()
    for i in items:
        assert i["store"] is None


async def test_shopping_store_delete_missing_is_zero_not_404(client):
    """Double-clicking the trash icon shouldn't see an error — a
    no-op delete on an unknown name returns 200 with cleared=0."""
    h = _auth(client._tok)
    r = await client.delete("/api/shopping/stores/Ghost", headers=h)
    assert r.status == 200
    assert (await r.json())["cleared"] == 0

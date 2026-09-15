"""Tests for socialhome.routes.shopping."""

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


async def test_shopping_post_with_store_round_trip(client):
    """POST with a store field persists it; GET returns it on every row."""
    r = await client.post(
        "/api/shopping",
        json={"text": "Bread", "store": "Bakery"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["store"] == "Bakery"

    r2 = await client.get("/api/shopping", headers=_auth(client._tok))
    items = await r2.json()
    bread = next(i for i in items if i["text"] == "Bread")
    assert bread["store"] == "Bakery"


async def test_shopping_get_includes_null_store(client):
    """Items without a store come back as ``"store": null`` (not omitted)
    so the SPA can patch a cache entry without checking key presence."""
    r = await client.post(
        "/api/shopping", json={"text": "Apples"}, headers=_auth(client._tok)
    )
    body = await r.json()
    assert "store" in body
    assert body["store"] is None


async def test_shopping_patch_item_text_and_store(client):
    """PATCH /api/shopping/{id} updates text and store."""
    r = await client.post(
        "/api/shopping",
        json={"text": "Milk", "store": "Aldi"},
        headers=_auth(client._tok),
    )
    item = await r.json()

    r2 = await client.patch(
        f"/api/shopping/{item['id']}",
        json={"text": "Whole Milk", "store": "Whole Foods"},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    body = await r2.json()
    assert body["text"] == "Whole Milk"
    assert body["store"] == "Whole Foods"


async def test_shopping_patch_clear_store_with_null(client):
    """PATCH with ``store: null`` clears the field."""
    r = await client.post(
        "/api/shopping",
        json={"text": "Eggs", "store": "Aldi"},
        headers=_auth(client._tok),
    )
    item = await r.json()

    r2 = await client.patch(
        f"/api/shopping/{item['id']}",
        json={"store": None},
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    body = await r2.json()
    assert body["store"] is None


async def test_shopping_patch_keep_store_when_omitted(client):
    """Omitting ``store`` in the body keeps the existing value."""
    r = await client.post(
        "/api/shopping",
        json={"text": "Apples", "store": "Aldi"},
        headers=_auth(client._tok),
    )
    item = await r.json()

    r2 = await client.patch(
        f"/api/shopping/{item['id']}",
        json={"text": "Green Apples"},
        headers=_auth(client._tok),
    )
    body = await r2.json()
    assert body["text"] == "Green Apples"
    assert body["store"] == "Aldi"


async def test_shopping_patch_unknown_returns_404(client):
    """PATCH on an unknown id returns 404."""
    r = await client.patch(
        "/api/shopping/no-such-id",
        json={"text": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_shopping_get_stores_returns_catalogue(client):
    """GET /api/shopping/stores returns the household catalogue in order."""
    await client.post(
        "/api/shopping",
        json={"text": "Bread", "store": "Bakery"},
        headers=_auth(client._tok),
    )
    await client.post(
        "/api/shopping",
        json={"text": "Milk", "store": "Aldi"},
        headers=_auth(client._tok),
    )

    r = await client.get("/api/shopping/stores", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert [s["name"] for s in body] == ["Bakery", "Aldi"]
    assert [s["sort_order"] for s in body] == [0, 1]


async def test_shopping_put_stores_order_reorders(client):
    """PUT /api/shopping/stores/order applies the new order and returns
    the canonical post-reorder list."""
    await client.post(
        "/api/shopping",
        json={"text": "Bread", "store": "Bakery"},
        headers=_auth(client._tok),
    )
    await client.post(
        "/api/shopping",
        json={"text": "Milk", "store": "Aldi"},
        headers=_auth(client._tok),
    )

    r = await client.put(
        "/api/shopping/stores/order",
        json={"order": ["Aldi", "Bakery"]},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert [s["name"] for s in body] == ["Aldi", "Bakery"]
    assert [s["sort_order"] for s in body] == [0, 1]


async def test_shopping_put_stores_order_non_array_rejected(client):
    """PUT with a non-array ``order`` returns 422."""
    r = await client.put(
        "/api/shopping/stores/order",
        json={"order": "Aldi"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_shopping_post_store_creates_catalogue_row(client):
    """POST /api/shopping/stores creates a store with no item attached."""
    r = await client.post(
        "/api/shopping/stores",
        json={"name": "Bakery"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["name"] == "Bakery"
    assert body["sort_order"] == 0

    stores = await (
        await client.get("/api/shopping/stores", headers=_auth(client._tok))
    ).json()
    assert [s["name"] for s in stores] == ["Bakery"]


async def test_shopping_post_store_is_idempotent_case_insensitively(client):
    """Re-POSTing a store under different casing returns the existing
    row unchanged rather than 409-ing or forking the catalogue."""
    h = _auth(client._tok)
    await client.post("/api/shopping/stores", json={"name": "Aldi"}, headers=h)
    r = await client.post("/api/shopping/stores", json={"name": "aldi"}, headers=h)
    assert r.status == 201
    body = await r.json()
    assert body["name"] == "Aldi"
    assert body["sort_order"] == 0

    stores = await (await client.get("/api/shopping/stores", headers=h)).json()
    assert [s["name"] for s in stores] == ["Aldi"]


async def test_shopping_post_store_blank_name_rejected(client):
    """A blank store name is a 422."""
    r = await client.post(
        "/api/shopping/stores",
        json={"name": "   "},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_shopping_store_rename_onto_existing_merges(client):
    """PATCH onto an existing store merges instead of 409-ing; the
    response reports the merge and how many items moved."""
    h = _auth(client._tok)
    await client.post(
        "/api/shopping", json={"text": "Milk", "store": "Aldi"}, headers=h
    )
    await client.post(
        "/api/shopping", json={"text": "Bread", "store": "Migros"}, headers=h
    )

    r = await client.patch(
        "/api/shopping/stores/Aldi", json={"name": "migros"}, headers=h
    )
    assert r.status == 200
    body = await r.json()
    assert body["old_name"] == "Aldi"
    assert body["new_name"] == "Migros"
    assert body["merged"] is True
    assert body["moved_items"] == 1

    stores = await (await client.get("/api/shopping/stores", headers=h)).json()
    assert [s["name"] for s in stores] == ["Migros"]
    items = await (await client.get("/api/shopping", headers=h)).json()
    assert {i["store"] for i in items} == {"Migros"}


async def test_shopping_store_rename_case_insensitive_lookup(client):
    """PATCH resolves the old store name case-insensitively and reports
    a plain (non-merge) rename."""
    h = _auth(client._tok)
    await client.post(
        "/api/shopping", json={"text": "Milk", "store": "Aldi"}, headers=h
    )

    r = await client.patch(
        "/api/shopping/stores/aldi", json={"name": "Coop"}, headers=h
    )
    assert r.status == 200
    body = await r.json()
    assert body["old_name"] == "Aldi"
    assert body["new_name"] == "Coop"
    assert body["merged"] is False
    assert body["moved_items"] == 1

"""HTTP tests for /api/push/* (Web Push subscription)."""

from __future__ import annotations


from .conftest import _auth


_VALID_SUB = {
    "endpoint": "https://push.example.com/abc",
    "keys": {"p256dh": "p256-key", "auth": "auth-secret"},
    "device_label": "Pascal's iPhone",
}


async def test_vapid_public_key_returns_string(client):
    r = await client.get("/api/push/vapid_public_key", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert isinstance(body["public_key"], str)
    assert len(body["public_key"]) > 20


async def test_vapid_public_key_requires_auth(client):
    r = await client.get("/api/push/vapid_public_key")
    assert r.status == 401


async def test_subscribe_creates_subscription(client):
    r = await client.post(
        "/api/push/subscribe",
        json=_VALID_SUB,
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["id"].startswith("sub-")


async def test_subscribe_missing_endpoint_returns_422(client):
    r = await client.post(
        "/api/push/subscribe",
        json={"keys": {"p256dh": "x", "auth": "y"}},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_subscribe_missing_keys_returns_422(client):
    r = await client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://x/y"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_list_subscriptions_excludes_secret_fields(client):
    r = await client.post(
        "/api/push/subscribe",
        json=_VALID_SUB,
        headers=_auth(client._tok),
    )
    sub_id = (await r.json())["id"]
    r = await client.get("/api/push/subscriptions", headers=_auth(client._tok))
    assert r.status == 200
    items = await r.json()
    item = next(i for i in items if i["id"] == sub_id)
    # Sensitive fields must NOT appear in the response.
    assert "endpoint" not in item
    assert "p256dh" not in item
    assert "auth" not in item
    # Safe fields may appear.
    assert item["device_label"] == "Pascal's iPhone"


async def test_unsubscribe_removes_subscription(client):
    r = await client.post(
        "/api/push/subscribe",
        json=_VALID_SUB,
        headers=_auth(client._tok),
    )
    sub_id = (await r.json())["id"]
    r = await client.delete(f"/api/push/subscribe/{sub_id}", headers=_auth(client._tok))
    assert r.status == 204
    # Second delete is 404.
    r = await client.delete(f"/api/push/subscribe/{sub_id}", headers=_auth(client._tok))
    assert r.status == 404


async def test_client_chosen_id_round_trips_subscribe_then_delete(client):
    """The SPA (``client/src/utils/webPush.ts``) sends its own
    endpoint-derived ``id`` on subscribe and deletes by that id when the
    user turns push off — the server row must actually go away."""
    h = _auth(client._tok)
    sub_id = "sub-0123456789abcdef0123456789abcdef"
    r = await client.post(
        "/api/push/subscribe", json={**_VALID_SUB, "id": sub_id}, headers=h
    )
    assert r.status == 201
    assert (await r.json())["id"] == sub_id
    r = await client.get("/api/push/subscriptions", headers=h)
    assert [s["id"] for s in await r.json()] == [sub_id]
    r = await client.delete(f"/api/push/subscribe/{sub_id}", headers=h)
    assert r.status == 204
    r = await client.get("/api/push/subscriptions", headers=h)
    assert await r.json() == []


async def test_unsubscribe_unknown_id_404(client):
    r = await client.delete("/api/push/subscribe/missing", headers=_auth(client._tok))
    assert r.status == 404

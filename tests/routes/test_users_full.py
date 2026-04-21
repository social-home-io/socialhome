"""Full route coverage for user endpoints."""

from .conftest import _auth


async def test_get_me_strips_sensitive(client):
    """GET /api/me returns user without sensitive fields."""
    h = _auth(client._tok)
    r = await client.get("/api/me", headers=h)
    body = await r.json()
    assert body["username"] == "admin"
    assert "password_hash" not in body
    assert "email" not in body


async def test_patch_me(client):
    """PATCH /api/me updates profile fields."""
    h = _auth(client._tok)
    r = await client.patch(
        "/api/me", json={"display_name": "Updated Admin", "bio": "test"}, headers=h
    )
    assert r.status == 200
    body = await r.json()
    assert body["display_name"] == "Updated Admin"


async def test_list_users(client):
    """GET /api/users returns user list."""
    h = _auth(client._tok)
    r = await client.get("/api/users", headers=h)
    assert r.status == 200
    assert len(await r.json()) >= 1


async def test_token_create_and_revoke(client):
    """POST /api/me/tokens + DELETE /api/me/tokens/{id}."""
    h = _auth(client._tok)
    r = await client.post("/api/me/tokens", json={"label": "test-device"}, headers=h)
    assert r.status == 201
    body = await r.json()
    assert "token" in body
    tid = body["token_id"]
    r2 = await client.delete(f"/api/me/tokens/{tid}", headers=h)
    assert r2.status in (200, 204)


async def test_no_auth_401(client):
    """Request without auth returns 401."""
    r = await client.get("/api/me")
    assert r.status == 401

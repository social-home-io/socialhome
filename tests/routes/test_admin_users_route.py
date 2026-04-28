"""Tests for POST /api/admin/users (standalone admin user creation)."""

from __future__ import annotations

from dataclasses import replace

from socialhome.app_keys import config_key


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_create_user_happy_path(client):
    r = await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice", "password": "hunter22"},
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["username"] == "alice"
    assert body["is_admin"] is False
    db = client._db
    pu = await db.fetchone(
        "SELECT * FROM platform_users WHERE username='alice'",
    )
    assert pu is not None
    user = await db.fetchone("SELECT * FROM users WHERE username='alice'")
    assert user is not None and user["is_admin"] == 0


async def test_create_user_with_is_admin_flag(client):
    r = await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice", "password": "hunter22", "is_admin": True},
    )
    assert r.status == 201
    user = await client._db.fetchone(
        "SELECT * FROM users WHERE username='alice'",
    )
    assert user["is_admin"] == 1


async def test_create_user_login_round_trip(client):
    """The created user can sign in via /api/auth/token immediately."""
    await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice", "password": "hunter22"},
    )
    r = await client.post(
        "/api/auth/token",
        json={"username": "alice", "password": "hunter22"},
    )
    assert r.status == 200, await r.text()
    body = await r.json()
    assert isinstance(body["token"], str) and len(body["token"]) > 20


async def test_create_user_duplicate_returns_409(client):
    await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice", "password": "hunter22"},
    )
    r = await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice", "password": "different"},
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "USERNAME_TAKEN"


async def test_create_user_short_password_422(client):
    r = await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice", "password": "short"},
    )
    assert r.status == 422


async def test_create_user_missing_fields_422(client):
    r = await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice"},
    )
    assert r.status == 422


async def test_create_user_non_admin_403(client):
    # Demote the test admin to a regular user.
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE username='admin'",
    )
    r = await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice", "password": "hunter22"},
    )
    assert r.status == 403


async def test_create_user_405_in_ha_mode(client):
    """ha and haos modes use /api/admin/ha-users/.../provision instead."""
    client.server.app[config_key] = replace(
        client.server.app[config_key],
        mode="ha",
    )
    r = await client.post(
        "/api/admin/users",
        headers=_auth(client._tok),
        json={"username": "alice", "password": "hunter22"},
    )
    assert r.status == 405
    assert (await r.json())["error"]["code"] == "WRONG_MODE"

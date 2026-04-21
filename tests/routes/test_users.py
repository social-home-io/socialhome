"""Tests for user routes — GET /api/me, PATCH /api/me, GET /api/users, tokens."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from social_home.app import create_app
from social_home.app_keys import db_key as _db_key
from social_home.auth import sha256_token_hash
from social_home.config import Config
from social_home.crypto import derive_user_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with admin user (pascal) and regular user (bob)."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        _row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'"
        )
        _pk = bytes.fromhex(_row["identity_public_key"])

        class _KP:
            public_key = _pk

        kp = _KP()
        uid = derive_user_id(kp.public_key, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", uid, "Pascal"),
        )
        raw_token = "test-token-raw"
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-1", uid, "test", sha256_token_hash(raw_token)),
        )
        uid2 = derive_user_id(kp.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", uid2, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-2", uid2, "test", sha256_token_hash("bob-token-raw")),
        )
        tc._admin_token = raw_token
        tc._admin_uid = uid
        tc._bob_token = "bob-token-raw"
        yield tc


async def test_get_me_returns_profile(client):
    """GET /api/me returns the current user's profile."""
    resp = await client.get("/api/me", headers=_auth(client._admin_token))
    assert resp.status == 200
    body = await resp.json()
    assert body["username"] == "pascal"
    assert body["is_admin"] is True


async def test_get_me_strips_sensitive_fields(client):
    """GET /api/me response does not contain sensitive fields like email or password_hash."""
    resp = await client.get("/api/me", headers=_auth(client._admin_token))
    body = await resp.json()
    assert "email" not in body
    assert "password_hash" not in body
    assert "identity_private_key" not in body


async def test_get_me_unauthorized(client):
    """GET /api/me returns 401 without authentication."""
    resp = await client.get("/api/me")
    assert resp.status == 401


async def test_patch_me_updates_display_name(client):
    """PATCH /api/me updates display_name and returns updated user."""
    resp = await client.patch(
        "/api/me",
        json={"display_name": "Pascal V."},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["display_name"] == "Pascal V."


async def test_list_users(client):
    """GET /api/users returns a list of at least 2 users."""
    resp = await client.get("/api/users", headers=_auth(client._admin_token))
    assert resp.status == 200
    body = await resp.json()
    assert len(body) >= 2


async def test_list_users_no_sensitive_fields(client):
    """GET /api/users response does not leak sensitive fields."""
    resp = await client.get("/api/users", headers=_auth(client._admin_token))
    body = await resp.json()
    for user in body:
        assert "email" not in user
        assert "password_hash" not in user


async def test_create_token(client):
    """POST /api/me/tokens creates a new API token and returns it."""
    resp = await client.post(
        "/api/me/tokens",
        json={"label": "laptop"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert "token" in body
    assert "token_id" in body


async def test_revoke_token(client):
    """DELETE /api/me/tokens/{id} revokes the specified token."""
    # Create a token first
    resp = await client.post(
        "/api/me/tokens",
        json={"label": "to-revoke"},
        headers=_auth(client._admin_token),
    )
    body = await resp.json()
    token_id = body["token_id"]
    # Revoke it
    resp2 = await client.delete(
        f"/api/me/tokens/{token_id}",
        headers=_auth(client._admin_token),
    )
    assert resp2.status in (200, 204)

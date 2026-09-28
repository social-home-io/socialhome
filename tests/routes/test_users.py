"""Tests for user routes — GET /api/me, PATCH /api/me, GET /api/users, tokens."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key, ws_manager_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id


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


# ─── PATCH /api/me — status (emoji + text + clear after) ─────────────────


class _Sock:
    closed = False

    def __init__(self):
        self.sent: list[dict] = []

    async def send_str(self, msg):
        self.sent.append(json.loads(msg))


async def _patch_me(client, body, token=None):
    return await client.patch(
        "/api/me", json=body, headers=_auth(token or client._admin_token)
    )


async def test_patch_me_sets_status_and_fires_frame(client):
    """The status keys are no longer ignored: they persist, GET /api/me
    returns them, and household tabs get ``user.status_changed``."""
    sock = _Sock()
    bob = await client.get("/api/me", headers=_auth(client._bob_token))
    await client.app[ws_manager_key].register((await bob.json())["user_id"], sock)

    resp = await _patch_me(client, {"status_emoji": "🌴", "status_text": "On leave"})
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == {"emoji": "🌴", "text": "On leave", "expires_at": None}

    me = await (await client.get("/api/me", headers=_auth(client._admin_token))).json()
    assert me["status"]["text"] == "On leave"

    frames = [f for f in sock.sent if f["type"] == "user.status_changed"]
    assert frames and frames[-1]["user_id"] == client._admin_uid
    assert frames[-1]["status"]["emoji"] == "🌴"


async def test_patch_me_status_clear_after_sets_expiry(client):
    before = datetime.now(timezone.utc)
    resp = await _patch_me(
        client,
        {"status_emoji": "🍽️", "status_text": "Lunch", "status_clear_after": "1h"},
    )
    assert resp.status == 200
    expires = datetime.fromisoformat((await resp.json())["status"]["expires_at"])
    assert timedelta(minutes=59) < expires - before <= timedelta(minutes=61)


async def test_patch_me_clear_after_alone_keeps_status(client):
    await _patch_me(client, {"status_emoji": "🎧", "status_text": "Focus"})
    resp = await _patch_me(client, {"status_clear_after": "30m"})
    assert resp.status == 200
    status = (await resp.json())["status"]
    assert (status["emoji"], status["text"]) == ("🎧", "Focus")
    assert status["expires_at"] is not None
    # And back to "never".
    status = (await (await _patch_me(client, {"status_clear_after": None})).json())[
        "status"
    ]
    assert status["expires_at"] is None and status["text"] == "Focus"


async def test_patch_me_clears_status(client):
    await _patch_me(client, {"status_emoji": "🎧", "status_text": "Focus"})
    resp = await _patch_me(client, {"status_emoji": None, "status_text": None})
    assert resp.status == 200
    assert (await resp.json())["status"] == {
        "emoji": None,
        "text": None,
        "expires_at": None,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"status_text": "x" * 81},
        {"status_text": "line one\nline two"},
        {"status_emoji": "busy"},
        {"status_emoji": "🎉", "status_clear_after": "forever"},
        {"status_emoji": "🎉", "status_clear_after": "2000-01-01T00:00:00+00:00"},
    ],
)
async def test_patch_me_invalid_status_is_422_and_saves_nothing(client, body):
    resp = await _patch_me(client, body)
    assert resp.status == 422
    me = await (await client.get("/api/me", headers=_auth(client._admin_token))).json()
    assert me["status"]["emoji"] is None and me["status"]["text"] is None


async def test_expired_status_reads_as_unset_before_sweep(client):
    """A status past its deadline is hidden at read time, even if the
    once-a-minute sweep hasn't cleared the row yet."""
    await client.app[_db_key].enqueue(
        "UPDATE users SET status_emoji='🎉', status_text='old', "
        "status_expires_at='2000-01-01T00:00:00+00:00' WHERE username='pascal'"
    )
    me = await (await client.get("/api/me", headers=_auth(client._admin_token))).json()
    assert me["status"] == {"emoji": None, "text": None, "expires_at": None}
    # Re-arming the expiry of an expired status doesn't resurrect it.
    resp = await _patch_me(client, {"status_clear_after": "1h"})
    assert (await resp.json())["status"]["text"] is None

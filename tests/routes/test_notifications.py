"""Tests for notification routes — /api/notifications/* endpoints."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with two users; notifications are triggered by creating posts."""
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
        tc._bob_uid = uid2
        yield tc


async def _trigger_notification(client):
    """Create a post to generate a notification for bob."""
    await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "notification trigger"},
        headers=_auth(client._admin_token),
    )


async def test_list_notifications(client):
    """GET /api/notifications returns a list (may be empty initially)."""
    resp = await client.get(
        "/api/notifications",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body, list)


async def test_list_notifications_after_trigger(client):
    """GET /api/notifications returns at least one notification after a post."""
    await _trigger_notification(client)
    resp = await client.get(
        "/api/notifications",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert len(body) >= 1


async def test_unread_count(client):
    """GET /api/notifications/unread-count returns an unread count."""
    await _trigger_notification(client)
    resp = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert "unread" in body
    assert body["unread"] >= 1


async def test_mark_single_read(client):
    """POST /api/notifications/{id}/read marks a specific notification read."""
    await _trigger_notification(client)
    notifs_resp = await client.get(
        "/api/notifications",
        headers=_auth(client._bob_token),
    )
    notifs = await notifs_resp.json()
    if notifs:
        notif_id = notifs[0]["id"]
        resp = await client.post(
            f"/api/notifications/{notif_id}/read",
            headers=_auth(client._bob_token),
        )
        assert resp.status == 200


async def test_mark_all_read(client):
    """POST /api/notifications/read-all marks all notifications read."""
    await _trigger_notification(client)
    resp = await client.post(
        "/api/notifications/read-all",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    # Unread count should now be 0
    resp2 = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._bob_token),
    )
    body = await resp2.json()
    assert body["unread"] == 0

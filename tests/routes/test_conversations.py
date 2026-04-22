"""Tests for conversation routes — /api/conversations/* endpoints."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id, generate_identity_keypair


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with admin (pascal) and regular user (bob)."""
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


async def test_create_dm(client):
    """POST /api/conversations/dm creates a DM and returns 201."""
    resp = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert "id" in body
    assert body["type"] == "dm"


async def test_send_message(client):
    """POST /api/conversations/{id}/messages sends a message and returns 201."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hello bob"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201


async def test_list_messages(client):
    """GET /api/conversations/{id}/messages returns messages in the conversation."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hello bob"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/conversations/{conv_id}/messages",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    msgs = await resp.json()
    assert len(msgs) == 1


async def test_mark_read(client):
    """POST /api/conversations/{id}/read marks the conversation as read."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    resp = await client.post(
        f"/api/conversations/{conv_id}/read",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200


async def test_unread_count(client):
    """GET /api/conversations/{id}/unread returns the unread message count."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/conversations/{conv_id}/unread",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert "unread" in body
    assert body["unread"] >= 1


async def test_create_group_dm(client):
    """POST /api/conversations/group creates a group DM and returns 201."""
    # Create a third user first (group DM requires at least 3 participants)
    from socialhome.app_keys import db_key as _db_key
    from socialhome.crypto import derive_user_id

    db = client.app[_db_key]
    kp = generate_identity_keypair()
    uid3 = derive_user_id(kp.public_key, "carol")
    await db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("carol", uid3, "Carol"),
    )
    resp = await client.post(
        "/api/conversations/group",
        json={"members": ["bob", "carol"], "name": "Team"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["type"] == "group_dm"


async def test_list_conversations(client):
    """GET /api/conversations lists the user's active conversations."""
    await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        "/api/conversations",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert len(body) >= 1


async def test_create_dm_with_self_is_error(client):
    """POST /api/conversations/dm with own username returns an error (422 or 404)."""
    resp = await client.post(
        "/api/conversations/dm",
        json={"username": "pascal"},
        headers=_auth(client._admin_token),
    )
    assert resp.status in (422, 404)


async def test_create_dm_nonexistent_user_404(client):
    """POST /api/conversations/dm with unknown username returns 404."""
    resp = await client.post(
        "/api/conversations/dm",
        json={"username": "nobody"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 404


async def test_send_empty_message_422(client):
    """POST messages with empty content returns 422."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": ""},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422

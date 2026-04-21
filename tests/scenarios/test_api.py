"""HTTP API integration tests using aiohttp TestClient.

Creates a real ``create_app()`` with a temp database, provisions users, and
hits the HTTP routes. This covers route handlers + auth middleware + services
+ repos in one shot.
"""

from __future__ import annotations


import pytest


from social_home.app import create_app
from social_home.app_keys import db_key as _db_key, space_service_key as _space_svc_key
from social_home.auth import sha256_token_hash
from social_home.config import Config
from social_home.crypto import (
    derive_user_id,
)


@pytest.fixture
async def client(aiohttp_client, tmp_dir):
    """Authenticated TestClient with admin + non-admin user."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)

    tc = await aiohttp_client(app)
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
        """INSERT INTO users(username, user_id, display_name, is_admin)
           VALUES(?,?,?,1)""",
        ("pascal", uid, "Pascal"),
    )

    raw_token = "test-token-raw"
    tok_hash = sha256_token_hash(raw_token)
    await db.enqueue(
        """INSERT INTO api_tokens(token_id, user_id, label, token_hash)
           VALUES(?,?,?,?)""",
        ("tid-1", uid, "test", tok_hash),
    )

    uid2 = derive_user_id(kp.public_key, "bob")
    await db.enqueue(
        """INSERT INTO users(username, user_id, display_name, is_admin)
           VALUES(?,?,?,0)""",
        ("bob", uid2, "Bob"),
    )
    raw_token2 = "bob-token-raw"
    await db.enqueue(
        """INSERT INTO api_tokens(token_id, user_id, label, token_hash)
           VALUES(?,?,?,?)""",
        ("tid-2", uid2, "test", sha256_token_hash(raw_token2)),
    )

    tc._admin_token = raw_token
    tc._admin_uid = uid
    tc._bob_token = raw_token2
    tc._bob_uid = uid2
    tc._space_svc = app[_space_svc_key]

    return tc


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── Health ───────────────────────────────────────────────────────────────


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"


# ─── Auth ─────────────────────────────────────────────────────────────────


async def test_auth_no_auth_returns_401(client):
    resp = await client.get("/api/me")
    assert resp.status == 401


async def test_auth_bad_token_returns_401(client):
    resp = await client.get("/api/me", headers=_auth("wrong"))
    assert resp.status == 401


async def test_auth_good_token(client):
    resp = await client.get("/api/me", headers=_auth(client._admin_token))
    assert resp.status == 200


async def test_auth_query_token(client):
    resp = await client.get(f"/api/me?token={client._admin_token}")
    assert resp.status == 200


# ─── Users ────────────────────────────────────────────────────────────────


async def test_users_get_me(client):
    resp = await client.get("/api/me", headers=_auth(client._admin_token))
    body = await resp.json()
    assert body["username"] == "pascal"
    assert body["is_admin"] is True
    # Sensitive fields stripped
    assert "email" not in body
    assert "password_hash" not in body


async def test_users_patch_me(client):
    resp = await client.patch(
        "/api/me",
        json={"display_name": "Pascal V."},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["display_name"] == "Pascal V."


async def test_users_list_users(client):
    resp = await client.get("/api/users", headers=_auth(client._admin_token))
    assert resp.status == 200
    body = await resp.json()
    assert len(body) >= 2


async def test_users_create_and_revoke_token(client):
    resp = await client.post(
        "/api/me/tokens",
        json={"label": "laptop"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert "token" in body
    assert "token_id" in body
    # Revoke
    resp2 = await client.delete(
        f"/api/me/tokens/{body['token_id']}",
        headers=_auth(client._admin_token),
    )
    assert resp2.status in (200, 204)


# ─── Feed ─────────────────────────────────────────────────────────────────


async def test_feed_create_and_list_post(client):
    resp = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "Hello world!"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    post = await resp.json()
    assert post["content"] == "Hello world!"

    resp2 = await client.get("/api/feed", headers=_auth(client._admin_token))
    assert resp2.status == 200
    feed = await resp2.json()
    assert any(p["id"] == post["id"] for p in feed)


async def test_feed_edit_and_delete(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "v1"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]

    r2 = await client.patch(
        f"/api/feed/posts/{post_id}",
        json={"content": "v2"},
        headers=_auth(client._admin_token),
    )
    assert r2.status == 200
    assert (await r2.json())["content"] == "v2"

    r3 = await client.delete(
        f"/api/feed/posts/{post_id}",
        headers=_auth(client._admin_token),
    )
    assert r3.status == 204


async def test_feed_reaction_add_remove(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "react me"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]

    r2 = await client.post(
        f"/api/feed/posts/{post_id}/reactions",
        json={"emoji": "👍"},
        headers=_auth(client._admin_token),
    )
    assert r2.status == 200

    r3 = await client.delete(
        f"/api/feed/posts/{post_id}/reactions/👍",
        headers=_auth(client._admin_token),
    )
    assert r3.status == 200


async def test_feed_comment_add_list(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "comment me"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]

    r2 = await client.post(
        f"/api/feed/posts/{post_id}/comments",
        json={"content": "nice post!"},
        headers=_auth(client._admin_token),
    )
    assert r2.status == 201

    r3 = await client.get(
        f"/api/feed/posts/{post_id}/comments",
        headers=_auth(client._admin_token),
    )
    assert r3.status == 200
    comments = await r3.json()
    assert len(comments) == 1


async def test_feed_non_author_cannot_edit(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "mine"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]

    r2 = await client.patch(
        f"/api/feed/posts/{post_id}",
        json={"content": "hijack"},
        headers=_auth(client._bob_token),
    )
    assert r2.status == 403


# ─── Notifications ────────────────────────────────────────────────────────


async def test_notifications_list_and_unread(client):
    # Create a post → bob should get a notification
    await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "hey"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        "/api/notifications",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert len(body) >= 1

    resp2 = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._bob_token),
    )
    body2 = await resp2.json()
    assert body2["unread"] >= 1


async def test_notifications_mark_all_read(client):
    await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "trigger"},
        headers=_auth(client._admin_token),
    )
    resp = await client.post(
        "/api/notifications/read-all",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    resp2 = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._bob_token),
    )
    assert (await resp2.json())["unread"] == 0


# ─── Conversations ────────────────────────────────────────────────────────


async def test_conversations_create_dm_and_send(client):
    resp = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    conv = await resp.json()

    resp2 = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"content": "hello bob"},
        headers=_auth(client._admin_token),
    )
    assert resp2.status == 201

    resp3 = await client.get(
        f"/api/conversations/{conv['id']}/messages",
        headers=_auth(client._admin_token),
    )
    assert resp3.status == 200
    msgs = await resp3.json()
    assert len(msgs) == 1


async def test_conversations_mark_read(client):
    resp = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await resp.json())["id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    resp2 = await client.post(
        f"/api/conversations/{conv_id}/read",
        headers=_auth(client._bob_token),
    )
    assert resp2.status == 200


async def test_conversations_list_conversations(client):
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


# ─── Spaces ──────────────────────────────────────────────────────────────


async def test_spaces_create_and_get(client):
    resp = await client.post(
        "/api/spaces",
        json={"name": "Family", "emoji": "🏠"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    space = await resp.json()
    assert space["name"] == "Family"

    resp2 = await client.get(
        f"/api/spaces/{space['id']}",
        headers=_auth(client._admin_token),
    )
    assert resp2.status == 200


async def test_spaces_update_and_dissolve(client):
    resp = await client.post(
        "/api/spaces",
        json={"name": "Temp"},
        headers=_auth(client._admin_token),
    )
    sid = (await resp.json())["id"]

    resp2 = await client.patch(
        f"/api/spaces/{sid}",
        json={"name": "Updated"},
        headers=_auth(client._admin_token),
    )
    assert resp2.status == 200
    assert (await resp2.json())["name"] == "Updated"

    resp3 = await client.delete(
        f"/api/spaces/{sid}",
        headers=_auth(client._admin_token),
    )
    assert resp3.status == 200


async def test_spaces_member_management(client):
    resp = await client.post(
        "/api/spaces",
        json={"name": "S"},
        headers=_auth(client._admin_token),
    )
    sid = (await resp.json())["id"]

    resp2 = await client.post(
        f"/api/spaces/{sid}/members",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    assert resp2.status == 201

    resp3 = await client.get(
        f"/api/spaces/{sid}/members",
        headers=_auth(client._admin_token),
    )
    members = await resp3.json()
    assert len(members) == 2

    resp4 = await client.delete(
        f"/api/spaces/{sid}/members/{client._bob_uid}",
        headers=_auth(client._admin_token),
    )
    assert resp4.status == 200


async def test_spaces_space_post(client):
    resp = await client.post(
        "/api/spaces",
        json={"name": "PostSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await resp.json())["id"]

    resp2 = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "space hello"},
        headers=_auth(client._admin_token),
    )
    assert resp2.status == 201

    resp3 = await client.get(
        f"/api/spaces/{sid}/feed",
        headers=_auth(client._admin_token),
    )
    assert resp3.status == 200
    feed = await resp3.json()
    assert len(feed) == 1


async def test_spaces_invite_token_flow(client):
    resp = await client.post(
        "/api/spaces",
        json={"name": "InviteSpace"},
        headers=_auth(client._admin_token),
    )
    sid = (await resp.json())["id"]

    resp2 = await client.post(
        f"/api/spaces/{sid}/invite-tokens",
        json={"uses": 1},
        headers=_auth(client._admin_token),
    )
    assert resp2.status == 201
    token = (await resp2.json())["token"]

    resp3 = await client.post(
        "/api/spaces/join",
        json={"token": token},
        headers=_auth(client._bob_token),
    )
    assert resp3.status == 200
    body = await resp3.json()
    assert body["role"] == "member"

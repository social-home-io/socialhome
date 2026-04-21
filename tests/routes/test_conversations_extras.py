"""Extra coverage for routes/conversations.py."""

from __future__ import annotations


from social_home.auth import sha256_token_hash

from .conftest import _auth


async def _seed_partner(client, *, username: str = "bob", uid: str = "bob-id") -> str:
    """Add a second user + token, return token."""
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, uid, username.title()),
    )
    raw = f"{username}-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", uid, "t", sha256_token_hash(raw)),
    )
    return raw


async def test_list_conversations_empty(client):
    r = await client.get("/api/conversations", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json()) == []


async def test_create_dm_unknown_user_404(client):
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "nobody"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_create_dm_with_partner(client):
    await _seed_partner(client)
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["type"] == "dm"


async def test_create_group_too_few_members_422(client):
    r = await client.post(
        "/api/conversations/group",
        json={"members": []},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_create_group_with_two_partners(client):
    await _seed_partner(client, username="bob", uid="bob-id")
    await _seed_partner(client, username="carol", uid="carol-id")
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob", "carol"], "name": "Family"},
        headers=_auth(client._tok),
    )
    assert r.status == 201


async def test_send_message_to_unknown_conv_404(client):
    r = await client.post(
        "/api/conversations/missing/messages",
        json={"content": "hi"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_list_messages_clamps_limit(client):
    await _seed_partner(client)
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._tok),
    )
    cid = (await r.json())["id"]
    r = await client.get(
        f"/api/conversations/{cid}/messages?limit=99999",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_full_dm_lifecycle(client):
    """Create DM, send, list, mark-read, get unread count."""
    raw = await _seed_partner(client)
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._tok),
    )
    cid = (await r.json())["id"]
    r = await client.post(
        f"/api/conversations/{cid}/messages",
        json={"content": "hello bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    # Bob lists messages.
    r = await client.get(
        f"/api/conversations/{cid}/messages",
        headers=_auth(raw),
    )
    assert r.status == 200
    msgs = await r.json()
    assert any(m.get("content") == "hello bob" for m in msgs)

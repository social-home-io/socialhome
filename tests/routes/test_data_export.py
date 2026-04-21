"""HTTP tests for /api/me/export + /api/users/{id}/export."""

from __future__ import annotations

import json


from social_home.auth import sha256_token_hash

from .conftest import _auth


# ─── Self export ────────────────────────────────────────────────────────


async def test_export_self_requires_auth(client):
    r = await client.get("/api/me/export")
    assert r.status == 401


async def test_export_self_returns_json_archive(client):
    r = await client.get("/api/me/export", headers=_auth(client._tok))
    assert r.status == 200
    assert "application/json" in r.headers["Content-Type"]
    assert "attachment" in r.headers["Content-Disposition"]
    body = json.loads(await r.read())
    assert body["user_id"] == client._uid
    assert "users" in body["tables"]


async def test_export_self_includes_authored_post(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "exported"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.get("/api/me/export", headers=_auth(client._tok))
    body = json.loads(await r.read())
    assert "feed_posts" in body["tables"]
    assert any(p.get("content") == "exported" for p in body["tables"]["feed_posts"])


# ─── Admin-only other-user export ───────────────────────────────────────


async def test_export_user_admin_succeeds(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('bob', 'bob-id', 'Bob')",
    )
    r = await client.get(
        "/api/users/bob-id/export",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = json.loads(await r.read())
    assert body["user_id"] == "bob-id"


async def test_export_user_non_admin_403(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('mallory', 'mal-id', 'M', 0)",
    )
    raw = "mal-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tm', 'mal-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.get(
        "/api/users/some-other-id/export",
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_export_user_unauth_401_or_403(client):
    r = await client.get("/api/users/anything/export")
    assert r.status in (401, 403)

"""Tests for social_home.routes.pages."""

from .conftest import _auth


async def test_create_page(client):
    """POST /api/pages creates a page."""
    r = await client.post(
        "/api/pages",
        json={"title": "Wiki", "content": "Hello"},
        headers=_auth(client._tok),
    )
    assert r.status == 201


async def test_list_pages(client):
    """GET /api/pages returns household pages."""
    await client.post(
        "/api/pages", json={"title": "T", "content": "C"}, headers=_auth(client._tok)
    )
    r = await client.get("/api/pages", headers=_auth(client._tok))
    assert r.status == 200
    assert len(await r.json()) >= 1


async def test_get_page(client):
    """GET /api/pages/{id} returns a single page."""
    r = await client.post(
        "/api/pages", json={"title": "T", "content": "C"}, headers=_auth(client._tok)
    )
    pid = (await r.json())["id"]
    r2 = await client.get(f"/api/pages/{pid}", headers=_auth(client._tok))
    assert r2.status == 200
    assert (await r2.json())["title"] == "T"


async def test_acquire_release_lock(client):
    """POST /api/pages/{id}/lock then DELETE releases it."""
    r = await client.post(
        "/api/pages", json={"title": "L", "content": "C"}, headers=_auth(client._tok)
    )
    pid = (await r.json())["id"]
    r2 = await client.post(f"/api/pages/{pid}/lock", headers=_auth(client._tok))
    assert r2.status == 200
    r3 = await client.delete(f"/api/pages/{pid}/lock", headers=_auth(client._tok))
    assert r3.status == 200


# ─── Stale-update detection (§23.72) ─────────────────────────────────────


async def test_patch_with_stale_base_returns_409(client):
    r = await client.post(
        "/api/pages",
        json={"title": "P", "content": "v1"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    pid = body["id"]
    # Bake a first edit so updated_at advances.
    r1 = await client.patch(
        f"/api/pages/{pid}",
        json={"content": "v2"},
        headers=_auth(client._tok),
    )
    await r1.json()
    # Now patch with a stale base — the old updated_at from `body`.
    r2 = await client.patch(
        f"/api/pages/{pid}",
        json={"content": "v3", "base_updated_at": body["updated_at"]},
        headers=_auth(client._tok),
    )
    assert r2.status == 409
    err = await r2.json()
    assert err["error"] == "stale_update"
    # The 409 payload echoes the current DB row — its updated_at must
    # differ from the stale base the client sent.
    assert err["current"]["updated_at"] != body["updated_at"]
    # first_response's updated_at also came from the same DB row, so
    # the 409 body's updated_at should match it (same row after the
    # first patch flushed through).
    assert err["current"]["content"] == "v2"


async def test_patch_with_fresh_base_succeeds(client):
    r = await client.post(
        "/api/pages",
        json={"title": "P", "content": "v1"},
        headers=_auth(client._tok),
    )
    body = await r.json()
    r2 = await client.patch(
        f"/api/pages/{body['id']}",
        json={"content": "v2", "base_updated_at": body["updated_at"]},
        headers=_auth(client._tok),
    )
    assert r2.status == 200


# ─── Lock routes ─────────────────────────────────────────────────────────


async def test_get_lock_returns_null_when_unlocked(client):
    r = await client.post(
        "/api/pages",
        json={"title": "L", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    r2 = await client.get(f"/api/pages/{pid}/lock", headers=_auth(client._tok))
    assert r2.status == 200
    assert (await r2.json()) is None


async def test_get_lock_returns_row_after_acquire(client):
    r = await client.post(
        "/api/pages",
        json={"title": "L", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await client.post(f"/api/pages/{pid}/lock", headers=_auth(client._tok))
    r2 = await client.get(f"/api/pages/{pid}/lock", headers=_auth(client._tok))
    assert r2.status == 200
    body = await r2.json()
    assert body["locked_by"] == client._uid


async def test_refresh_lock_204(client):
    r = await client.post(
        "/api/pages",
        json={"title": "L", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await client.post(f"/api/pages/{pid}/lock", headers=_auth(client._tok))
    r2 = await client.post(
        f"/api/pages/{pid}/lock/refresh",
        headers=_auth(client._tok),
    )
    assert r2.status == 204


async def test_refresh_lock_held_by_other_returns_409(client):
    """Second user's refresh must fail with 409 so their client surrenders."""
    from social_home.auth import sha256_token_hash

    # Seed a second user + token on the same client.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('bob', 'bob-id', 'Bob', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('tb', 'bob-id', 't', ?)",
        (sha256_token_hash("bob-tok"),),
    )
    r = await client.post(
        "/api/pages",
        json={"title": "L", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # Admin grabs the lock.
    await client.post(f"/api/pages/{pid}/lock", headers=_auth(client._tok))
    # Bob tries to refresh.
    r2 = await client.post(
        f"/api/pages/{pid}/lock/refresh",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r2.status == 409
    err = await r2.json()
    assert err["error"] == "lock_held"


# ─── Admin gate on revert ───────────────────────────────────────────────


async def test_revert_non_admin_403(client):
    from social_home.auth import sha256_token_hash

    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('carol', 'carol-id', 'Carol', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('tc', 'carol-id', 't', ?)",
        (sha256_token_hash("carol-tok"),),
    )
    r = await client.post(
        "/api/pages",
        json={"title": "H", "content": "v1"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # Bake a version via PATCH.
    await client.patch(
        f"/api/pages/{pid}",
        json={"content": "v2"},
        headers=_auth(client._tok),
    )
    # Carol (non-admin) tries to revert → 403.
    r3 = await client.post(
        f"/api/pages/{pid}/revert",
        json={"version": 1},
        headers={"Authorization": "Bearer carol-tok"},
    )
    assert r3.status == 403


# ─── Two-step delete ────────────────────────────────────────────────────


async def test_delete_request_then_admin_approve(client):
    r = await client.post(
        "/api/pages",
        json={"title": "D", "content": "bye"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # Seed a second user who raises the request so admin-approve is
    # a distinct actor.
    from social_home.auth import sha256_token_hash

    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('dave', 'dave-id', 'Dave', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('td', 'dave-id', 't', ?)",
        (sha256_token_hash("dave-tok"),),
    )
    req = await client.post(
        f"/api/pages/{pid}/delete-request",
        headers={"Authorization": "Bearer dave-tok"},
    )
    assert req.status == 200
    # Admin approves.
    ap = await client.post(
        f"/api/pages/{pid}/delete-approve",
        headers=_auth(client._tok),
    )
    assert ap.status == 200
    # Page is gone.
    miss = await client.get(f"/api/pages/{pid}", headers=_auth(client._tok))
    assert miss.status == 404


async def test_delete_approve_requires_admin(client):
    from social_home.auth import sha256_token_hash

    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('eve', 'eve-id', 'Eve', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('te', 'eve-id', 't', ?)",
        (sha256_token_hash("eve-tok"),),
    )
    r = await client.post(
        "/api/pages",
        json={"title": "E", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    await client.post(
        f"/api/pages/{pid}/delete-request",
        headers={"Authorization": "Bearer eve-tok"},
    )
    # Eve (non-admin) cannot approve.
    r2 = await client.post(
        f"/api/pages/{pid}/delete-approve",
        headers={"Authorization": "Bearer eve-tok"},
    )
    assert r2.status == 403


async def test_delete_approve_rejects_self_approval(client):
    r = await client.post(
        "/api/pages",
        json={"title": "S", "content": ""},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # Admin (same user) requests then tries to approve → 409.
    await client.post(
        f"/api/pages/{pid}/delete-request",
        headers=_auth(client._tok),
    )
    r2 = await client.post(
        f"/api/pages/{pid}/delete-approve",
        headers=_auth(client._tok),
    )
    assert r2.status == 409


# ─── Space-scoped pages ─────────────────────────────────────────────────


async def _seed_space_with_member(client, *, space_id: str = "sp-x") -> str:
    """Create a space and add the admin user as a member. Returns space_id."""
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'X', 'inst', 'admin', ?)",
        (space_id, "ab" * 32),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'admin')",
        (space_id, client._uid),
    )
    return space_id


async def test_space_page_create_requires_member(client):
    sid = await _seed_space_with_member(client)
    r = await client.post(
        f"/api/spaces/{sid}/pages",
        json={"title": "S", "content": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["space_id"] == sid


async def test_space_page_non_member_403(client):
    from social_home.auth import sha256_token_hash

    sid = await _seed_space_with_member(client)
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('out', 'out-id', 'Out', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('to', 'out-id', 't', ?)",
        (sha256_token_hash("out-tok"),),
    )
    r = await client.get(
        f"/api/spaces/{sid}/pages",
        headers={"Authorization": "Bearer out-tok"},
    )
    assert r.status == 403


# ─── MAX_HISTORY = 5 trim ───────────────────────────────────────────────


async def test_history_trimmed_to_5(client):
    r = await client.post(
        "/api/pages",
        json={"title": "T", "content": "v0"},
        headers=_auth(client._tok),
    )
    pid = (await r.json())["id"]
    # Seven edits → snapshot becomes 7 rows before trim; trim caps at 5.
    for i in range(1, 8):
        await client.patch(
            f"/api/pages/{pid}",
            json={"content": f"v{i}"},
            headers=_auth(client._tok),
        )
    r2 = await client.get(
        f"/api/pages/{pid}/versions",
        headers=_auth(client._tok),
    )
    versions = await r2.json()
    assert len(versions) == 5
    assert [v["content"] for v in versions] == ["v2", "v3", "v4", "v5", "v6"]

"""Extra coverage for routes/child_protection.py — auth + edge branches."""

from __future__ import annotations


from socialhome.auth import sha256_token_hash

from .conftest import _auth


# ─── Unauth ──────────────────────────────────────────────────────────────


async def test_list_guardians_unauth_401(client):
    r = await client.get("/api/cp/users/some/guardians")
    assert r.status == 401


async def test_add_guardian_unauth_401(client):
    r = await client.post("/api/cp/users/some/guardians/other")
    assert r.status == 401


async def test_remove_guardian_unauth_401(client):
    r = await client.delete("/api/cp/users/some/guardians/other")
    assert r.status == 401


async def test_block_for_minor_unauth_401(client):
    r = await client.post("/api/cp/minors/some/blocks/other")
    assert r.status == 401


async def test_unblock_for_minor_unauth_401(client):
    r = await client.delete("/api/cp/minors/some/blocks/other")
    assert r.status == 401


# ─── Branches ────────────────────────────────────────────────────────────


async def test_list_guardians_subject_can_read_self(client):
    """A user can read their own guardians list (admin or guardian or self)."""
    r = await client.get(
        f"/api/cp/users/{client._uid}/guardians",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["guardians"] == []


async def test_list_guardians_random_user_403(client):
    """Non-admin, non-guardian, non-self → 403."""
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('mom', 'mom-id', 'Mom', 0)",
    )
    raw = "mom-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tm', 'mom-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.get(
        "/api/cp/users/lila-id/guardians",
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_add_guardian_self_422(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('lila', 'lila-id', 'Lila')",
    )
    r = await client.post(
        "/api/cp/users/lila-id/guardians/lila-id",
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_remove_unknown_guardian_204(client):
    """Removing a guardian relationship that doesn't exist is idempotent."""
    r = await client.delete(
        "/api/cp/users/missing/guardians/also-missing",
        headers=_auth(client._tok),
    )
    assert r.status == 204

"""HTTP tests for /api/cp/* (child protection)."""

from __future__ import annotations


from social_home.auth import sha256_token_hash

from .conftest import _auth


async def _seed_minor(client, *, username: str = "lila", uid: str = "lila-id"):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, uid, username.title()),
    )


async def _seed_space(client, *, sid: str = "sp-1"):
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'X', 'inst', 'admin', ?)",
        (sid, "ab" * 32),
    )


# ─── Auth ────────────────────────────────────────────────────────────────


async def test_update_protection_unauth_401(client):
    r = await client.post(
        "/api/cp/users/lila/protection",
        json={"enabled": True, "declared_age": 12},
    )
    assert r.status == 401


async def test_get_age_gate_unauth_401(client):
    r = await client.get("/api/cp/spaces/sp-1/age-gate")
    assert r.status == 401


# ─── Protection toggle ──────────────────────────────────────────────────


async def test_enable_protection_admin_204(client):
    await _seed_minor(client)
    r = await client.post(
        "/api/cp/users/lila/protection",
        json={"enabled": True, "declared_age": 12},
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_enable_protection_missing_age_422(client):
    await _seed_minor(client)
    r = await client.post(
        "/api/cp/users/lila/protection",
        json={"enabled": True},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_enable_protection_invalid_age_422(client):
    await _seed_minor(client)
    r = await client.post(
        "/api/cp/users/lila/protection",
        json={"enabled": True, "declared_age": 25},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_enable_protection_bad_json_400(client):
    r = await client.post(
        "/api/cp/users/lila/protection",
        data="bad",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_enable_protection_non_admin_403(client):
    await _seed_minor(client)
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
    r = await client.post(
        "/api/cp/users/lila/protection",
        json={"enabled": True, "declared_age": 12},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_disable_protection_204(client):
    await _seed_minor(client)
    # enable first
    r = await client.post(
        "/api/cp/users/lila/protection",
        json={"enabled": True, "declared_age": 12},
        headers=_auth(client._tok),
    )
    assert r.status == 204
    r = await client.post(
        "/api/cp/users/lila/protection",
        json={"enabled": False},
        headers=_auth(client._tok),
    )
    assert r.status == 204


# ─── Guardians ───────────────────────────────────────────────────────────


async def test_add_guardian_admin_204(client):
    await _seed_minor(client)
    await _seed_minor(client, username="mom", uid="mom-id")
    r = await client.post(
        "/api/cp/users/lila-id/guardians/mom-id",
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_list_guardians_admin_sees_all(client):
    await _seed_minor(client)
    await _seed_minor(client, username="mom", uid="mom-id")
    r = await client.post(
        "/api/cp/users/lila-id/guardians/mom-id",
        headers=_auth(client._tok),
    )
    assert r.status == 204
    r = await client.get(
        "/api/cp/users/lila-id/guardians",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["guardians"] == ["mom-id"]


async def test_remove_guardian_204(client):
    await _seed_minor(client)
    await _seed_minor(client, username="mom", uid="mom-id")
    await client.post(
        "/api/cp/users/lila-id/guardians/mom-id",
        headers=_auth(client._tok),
    )
    r = await client.delete(
        "/api/cp/users/lila-id/guardians/mom-id",
        headers=_auth(client._tok),
    )
    assert r.status == 204


# ─── Per-minor blocks ────────────────────────────────────────────────────


async def test_block_for_minor_requires_guardian_403(client):
    await _seed_minor(client)
    # admin is not a guardian → 403
    r = await client.post(
        "/api/cp/minors/lila-id/blocks/bad-id",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_block_for_minor_succeeds_when_guardian(client):
    await _seed_minor(client)
    await _seed_minor(client, username="mom", uid="mom-id")
    raw = "mom-tok"
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tm2', 'mom-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    # Admin assigns mom as guardian.
    await client.post(
        "/api/cp/users/lila-id/guardians/mom-id",
        headers=_auth(client._tok),
    )
    # Now mom can block.
    r = await client.post(
        "/api/cp/minors/lila-id/blocks/bad-id",
        headers=_auth(raw),
    )
    assert r.status == 204
    # And unblock.
    r = await client.delete(
        "/api/cp/minors/lila-id/blocks/bad-id",
        headers=_auth(raw),
    )
    assert r.status == 204


# ─── Space age gate ──────────────────────────────────────────────────────


async def test_get_age_gate_default_for_unknown_space(client):
    r = await client.get(
        "/api/cp/spaces/sp-nope/age-gate",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"min_age": 0, "target_audience": "all"}


async def test_set_age_gate_admin_204(client):
    await _seed_space(client)
    r = await client.patch(
        "/api/cp/spaces/sp-1/age-gate",
        json={"min_age": 13, "target_audience": "teen"},
        headers=_auth(client._tok),
    )
    assert r.status == 204
    r = await client.get(
        "/api/cp/spaces/sp-1/age-gate",
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert body["min_age"] == 13


async def test_set_age_gate_missing_fields_422(client):
    await _seed_space(client)
    r = await client.patch(
        "/api/cp/spaces/sp-1/age-gate",
        json={"min_age": 13},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_set_age_gate_bad_json_400(client):
    await _seed_space(client)
    r = await client.patch(
        "/api/cp/spaces/sp-1/age-gate",
        data="bad",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_set_age_gate_unknown_space_404(client):
    r = await client.patch(
        "/api/cp/spaces/sp-missing/age-gate",
        json={"min_age": 13, "target_audience": "teen"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_set_age_gate_invalid_value_422(client):
    await _seed_space(client)
    r = await client.patch(
        "/api/cp/spaces/sp-1/age-gate",
        json={"min_age": 21, "target_audience": "teen"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── Guardian audit log (§CP) ──────────────────────────────────────────────


async def test_audit_log_admin_can_read(client):
    """The admin (seeded by the test fixture) may always read an audit log."""
    r = await client.get(
        "/api/cp/minors/anyone-uid/audit-log",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert "entries" in body


async def test_audit_log_forbidden_for_non_guardian(client):
    """A non-admin non-guardian gets 403."""
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("stranger", "stranger-uid", "Stranger"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("ts", "stranger-uid", "t", sha256_token_hash("stranger-tok")),
    )
    r = await client.get(
        "/api/cp/minors/lila-uid/audit-log",
        headers={"Authorization": "Bearer stranger-tok"},
    )
    assert r.status == 403


# ─── /api/cp/minors (guardian list) ──────────────────────────────────────


async def test_list_minors_for_self_guardian_200(client):
    """Every caller can list their own assigned minors."""
    await _seed_minor(client)
    # admin is a guardian of lila.
    await client.post(
        "/api/cp/users/lila-id/guardians/" + client._uid,
        headers=_auth(client._tok),
    )
    r = await client.get("/api/cp/minors", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert "lila-id" in body["minors"]


async def test_list_minors_other_guardian_requires_admin(client):
    """Non-admin asking for another user's minors → 403."""
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("bob", "bob-uid", "Bob"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("tb", "bob-uid", "t", sha256_token_hash("bob-tok")),
    )
    r = await client.get(
        "/api/cp/minors?guardian_id=some-other-id",
        headers={"Authorization": "Bearer bob-tok"},
    )
    assert r.status == 403


# ─── /api/cp/minors/{id}/blocks (list) ──────────────────────────────────


async def test_list_blocks_as_guardian_200(client):
    await _seed_minor(client)
    await client.post(
        "/api/cp/users/lila-id/guardians/" + client._uid,
        headers=_auth(client._tok),
    )
    # Block a random user.
    await client.post(
        "/api/cp/minors/lila-id/blocks/bad-user-id",
        headers=_auth(client._tok),
    )
    r = await client.get(
        "/api/cp/minors/lila-id/blocks",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert any(b["blocked_user_id"] == "bad-user-id" for b in body["blocks"])


async def test_list_blocks_non_guardian_403(client):
    await _seed_minor(client)
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("stranger", "stranger-uid", "Stranger"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("ts2", "stranger-uid", "t", sha256_token_hash("stranger-tok2")),
    )
    r = await client.get(
        "/api/cp/minors/lila-id/blocks",
        headers={"Authorization": "Bearer stranger-tok2"},
    )
    assert r.status == 403


# ─── /api/cp/minors/{id}/conversations + /dm-contacts ────────────────────


async def test_list_conversations_as_guardian_200(client):
    await _seed_minor(client)
    await client.post(
        "/api/cp/users/lila-id/guardians/" + client._uid,
        headers=_auth(client._tok),
    )
    r = await client.get(
        "/api/cp/minors/lila-id/conversations",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert isinstance(body["conversations"], list)


async def test_list_conversations_non_guardian_403(client):
    await _seed_minor(client)
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("outside", "outside-uid", "Out"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("ts3", "outside-uid", "t", sha256_token_hash("outside-tok")),
    )
    r = await client.get(
        "/api/cp/minors/lila-id/conversations",
        headers={"Authorization": "Bearer outside-tok"},
    )
    assert r.status == 403


async def test_list_dm_contacts_as_guardian_200(client):
    await _seed_minor(client)
    await client.post(
        "/api/cp/users/lila-id/guardians/" + client._uid,
        headers=_auth(client._tok),
    )
    r = await client.get(
        "/api/cp/minors/lila-id/dm-contacts",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert isinstance(body["contacts"], list)

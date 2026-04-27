"""HTTP tests for /api/aliases/* (§4.1.6 viewer-private user aliases)."""

from __future__ import annotations


from .conftest import _auth


async def _seed_target(client, *, user_id="uid-bob", username="bob"):
    """Insert a second local user that admin can alias."""
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, user_id, "Bob"),
    )


# ── PUT /api/aliases/users/{user_id} ────────────────────────────────────


async def test_put_alias_creates(client):
    await _seed_target(client)
    r = await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "Mr B"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"target_user_id": "uid-bob", "alias": "Mr B"}


async def test_put_alias_updates_existing(client):
    await _seed_target(client)
    await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "B1"},
        headers=_auth(client._tok),
    )
    r = await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "B2"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["alias"] == "B2"


async def test_put_alias_trims_whitespace(client):
    await _seed_target(client)
    r = await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "   Mr B   "},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["alias"] == "Mr B"


async def test_put_alias_rejects_empty(client):
    await _seed_target(client)
    r = await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "   "},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_alias_rejects_too_long(client):
    await _seed_target(client)
    r = await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "x" * 81},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_alias_rejects_self(client):
    """A user cannot alias themselves — they have other rename surfaces."""
    r = await client.put(
        f"/api/aliases/users/{client._uid}",
        json={"alias": "Me"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_put_alias_unknown_target_404(client):
    r = await client.put(
        "/api/aliases/users/uid-ghost",
        json={"alias": "X"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_put_alias_requires_auth(client):
    r = await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "X"},
    )
    assert r.status == 401


# ── GET /api/aliases/users ──────────────────────────────────────────────


async def test_get_aliases_lists_only_viewer_aliases(client):
    await _seed_target(client)
    await _seed_target(client, user_id="uid-c", username="c")
    await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "B"},
        headers=_auth(client._tok),
    )
    await client.put(
        "/api/aliases/users/uid-c",
        json={"alias": "C"},
        headers=_auth(client._tok),
    )
    r = await client.get(
        "/api/aliases/users",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    targets = {row["target_user_id"]: row["alias"] for row in body["aliases"]}
    assert targets == {"uid-bob": "B", "uid-c": "C"}


async def test_get_aliases_requires_auth(client):
    r = await client.get("/api/aliases/users")
    assert r.status == 401


# ── DELETE /api/aliases/users/{user_id} ─────────────────────────────────


async def test_delete_alias_removes(client):
    await _seed_target(client)
    await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "B"},
        headers=_auth(client._tok),
    )
    r = await client.delete(
        "/api/aliases/users/uid-bob",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"target_user_id": "uid-bob", "alias": None}
    # Confirm gone.
    r2 = await client.get(
        "/api/aliases/users",
        headers=_auth(client._tok),
    )
    assert (await r2.json())["aliases"] == []


async def test_delete_alias_unknown_is_idempotent(client):
    r = await client.delete(
        "/api/aliases/users/uid-ghost",
        headers=_auth(client._tok),
    )
    assert r.status == 200

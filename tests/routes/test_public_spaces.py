"""HTTP tests for /api/public_spaces."""

from __future__ import annotations


from socialhome.auth import sha256_token_hash
from socialhome.repositories.public_space_repo import (
    PublicSpaceListing,
    SqlitePublicSpaceRepo,
)

from .conftest import _auth


async def _seed(
    client,
    *,
    space_id: str = "sp-1",
    instance_id: str = "remote-1",
    member_count: int = 5,
):
    repo = SqlitePublicSpaceRepo(client._db)
    await repo.upsert(
        PublicSpaceListing(
            space_id=space_id,
            instance_id=instance_id,
            name=f"Space {space_id}",
            member_count=member_count,
        )
    )


# ─── List ────────────────────────────────────────────────────────────────


async def test_list_requires_auth(client):
    r = await client.get("/api/public_spaces")
    assert r.status == 401


async def test_list_empty(client):
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json()) == []


async def test_list_returns_seeded_listings(client):
    await _seed(client, space_id="sp-A")
    await _seed(client, space_id="sp-B", member_count=99)
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    body = await r.json()
    ids = [s["space_id"] for s in body]
    assert "sp-A" in ids and "sp-B" in ids
    # Higher member_count first.
    assert ids.index("sp-B") < ids.index("sp-A")


async def test_list_clamps_limit(client):
    for i in range(10):
        await _seed(client, space_id=f"sp-{i}")
    r = await client.get(
        "/api/public_spaces?limit=99999",
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert len(body) <= 200


async def test_list_invalid_limit_falls_back(client):
    r = await client.get(
        "/api/public_spaces?limit=not-a-number",
        headers=_auth(client._tok),
    )
    assert r.status == 200


# ─── Hide ────────────────────────────────────────────────────────────────


async def test_hide_removes_from_visible_list(client):
    await _seed(client, space_id="sp-1")
    await _seed(client, space_id="sp-2")
    r = await client.post(
        "/api/public_spaces/sp-1/hide",
        headers=_auth(client._tok),
    )
    assert r.status == 204
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    body = await r.json()
    assert all(s["space_id"] != "sp-1" for s in body)


async def test_hide_requires_auth(client):
    r = await client.post("/api/public_spaces/sp-1/hide")
    assert r.status == 401


# ─── Block instance ─────────────────────────────────────────────────────


async def test_block_instance_admin_succeeds(client):
    r = await client.post(
        "/api/public_spaces/blocked_instances/bad-inst",
        json={"reason": "spam"},
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_block_instance_non_admin_403(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES('bob3', 'bob3-id', 'Bob', 0)",
    )
    raw = "bob3-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES('tb3', 'bob3-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await client.post(
        "/api/public_spaces/blocked_instances/some-inst",
        json={},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_block_then_list_excludes_blocked_instance(client):
    await _seed(client, space_id="sp-A", instance_id="bad-inst")
    await _seed(client, space_id="sp-B", instance_id="ok-inst")
    r = await client.post(
        "/api/public_spaces/blocked_instances/bad-inst",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 204
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    body = await r.json()
    assert all(s["instance_id"] != "bad-inst" for s in body)


# ─── §CP.F1 — minor discovery filter ────────────────────────────────────


async def _seed_with_age(client, *, space_id: str, min_age: int, target: str = "all"):
    repo = SqlitePublicSpaceRepo(client._db)
    await repo.upsert(
        PublicSpaceListing(
            space_id=space_id,
            instance_id="remote-1",
            name=f"Space {space_id}",
            min_age=min_age,
            target_audience=target,
        )
    )


async def _seed_minor(client, *, declared_age: int) -> str:
    """Add a protected-minor user with their own API token. Returns the token."""
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin,"
        " is_minor, child_protection_enabled, declared_age)"
        " VALUES('kid', 'kid-id', 'Kid', 0, 1, 1, ?)",
        (declared_age,),
    )
    tok = "kid-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tk', 'kid-id', 't', ?)",
        (sha256_token_hash(tok),),
    )
    return tok


async def test_minor_is_hidden_from_age_gated_listings(client):
    await _seed_with_age(client, space_id="sp-adult", min_age=18)
    await _seed_with_age(client, space_id="sp-teen", min_age=13)
    await _seed_with_age(client, space_id="sp-open", min_age=0)
    tok = await _seed_minor(client, declared_age=12)
    r = await client.get(
        "/api/public_spaces",
        headers={"Authorization": f"Bearer {tok}"},
    )
    body = await r.json()
    ids = {s["space_id"] for s in body}
    assert "sp-open" in ids
    assert "sp-teen" not in ids
    assert "sp-adult" not in ids


async def test_adult_sees_all_listings(client):
    await _seed_with_age(client, space_id="sp-adult", min_age=18)
    await _seed_with_age(client, space_id="sp-open", min_age=0)
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    body = await r.json()
    ids = {s["space_id"] for s in body}
    assert {"sp-adult", "sp-open"} <= ids
    # Payload exposes min_age so UI can show the age badge.
    adult = next(s for s in body if s["space_id"] == "sp-adult")
    assert adult["min_age"] == 18
    assert adult["target_audience"] == "all"

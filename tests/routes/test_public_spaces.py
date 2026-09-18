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
    join_mode: str = "invite_only",
    allow_subscribers: bool = False,
):
    repo = SqlitePublicSpaceRepo(client._db)
    await repo.upsert(
        PublicSpaceListing(
            space_id=space_id,
            instance_id=instance_id,
            name=f"Space {space_id}",
            member_count=member_count,
            join_mode=join_mode,
            allow_subscribers=allow_subscribers,
        )
    )


async def test_list_returns_the_real_join_mode(client):
    """The directory now carries the host's real ``join_mode`` — the SPA used
    to fabricate ``'request'`` for every listing because the field was absent,
    and so offered Subscribe on spaces nobody may read."""
    await _seed(client, space_id="sp-open", join_mode="open")
    await _seed(client, space_id="sp-inv", join_mode="invite_only")
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    modes = {s["space_id"]: s["join_mode"] for s in await r.json()}
    assert modes["sp-open"] == "open"
    assert modes["sp-inv"] == "invite_only"


async def test_list_join_mode_fails_closed_for_an_unknown_value(client):
    """A directory row whose join mode is unknown (an older GFS, a hostile
    one) reads as invite-only — never as something more permissive."""
    await _seed(client, space_id="sp-weird", join_mode="everyone")
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    entry = next(s for s in await r.json() if s["space_id"] == "sp-weird")
    assert entry["join_mode"] == "invite_only"


async def test_list_returns_allow_subscribers(client):
    """The browser needs the host's readability opt-in BEFORE any local row
    for the space exists — it is what decides whether Subscribe is offered.
    Note ``sp-broadcast`` is invite-only AND readable: the two dials are
    independent."""
    await _seed(
        client,
        space_id="sp-broadcast",
        join_mode="invite_only",
        allow_subscribers=True,
    )
    await _seed(
        client,
        space_id="sp-open-private",
        join_mode="open",
        allow_subscribers=False,
    )
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    rows = {s["space_id"]: s for s in await r.json()}
    assert rows["sp-broadcast"]["allow_subscribers"] is True
    assert rows["sp-broadcast"]["join_mode"] == "invite_only"
    assert rows["sp-open-private"]["allow_subscribers"] is False
    assert rows["sp-open-private"]["join_mode"] == "open"


async def test_list_allow_subscribers_defaults_closed(client):
    """A row cached before migration 0051 (or by an older GFS that reports
    no flag) reads as not-readable — never as something more permissive."""
    await _seed(client, space_id="sp-nodata")
    r = await client.get("/api/public_spaces", headers=_auth(client._tok))
    entry = next(s for s in await r.json() if s["space_id"] == "sp-nodata")
    assert entry["allow_subscribers"] is False


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


# ─── Join request (POST /api/public_spaces/{id}/join-request) ────────────


async def test_join_request_requires_auth(client):
    r = await client.post(
        "/api/public_spaces/sp-1/join-request",
        json={"host_instance_id": "remote-1"},
    )
    assert r.status == 401


async def test_join_request_requires_host_instance_id(client):
    r = await client.post(
        "/api/public_spaces/sp-1/join-request",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_join_request_paired_host_federates(client, monkeypatch):
    """A paired-host global space federates the join request via
    ``request_join_remote`` and returns 202 with the request id."""
    from socialhome.services.space_service import SpaceService

    captured: dict = {}

    async def _fake(
        self,
        space_id,
        *,
        applicant_user_id,
        host_instance_id,
        message=None,
    ):
        captured.update(
            space_id=space_id,
            applicant_user_id=applicant_user_id,
            host_instance_id=host_instance_id,
            message=message,
        )
        return "req-xyz"

    monkeypatch.setattr(SpaceService, "request_join_remote", _fake)
    r = await client.post(
        "/api/public_spaces/sp-global/join-request",
        json={"host_instance_id": "remote-1", "message": "hi"},
        headers=_auth(client._tok),
    )
    assert r.status == 202
    assert (await r.json())["request_id"] == "req-xyz"
    assert captured["space_id"] == "sp-global"
    assert captured["host_instance_id"] == "remote-1"
    assert captured["applicant_user_id"] == client._uid
    assert captured["message"] == "hi"


async def test_join_request_unpaired_host_403(client, monkeypatch):
    """An unpaired host can't be join-requested — ``request_join_remote``
    raises ``SpacePermissionError`` ("pair first"), which the BaseView
    exception map surfaces as 403 FORBIDDEN so the SPA can fall back to
    the pairing flow."""
    from socialhome.domain.space import SpacePermissionError
    from socialhome.services.space_service import SpaceService

    async def _fake(
        self, space_id, *, applicant_user_id, host_instance_id, message=None
    ):
        raise SpacePermissionError(
            "host household is not a CONFIRMED peer — pair first",
        )

    monkeypatch.setattr(SpaceService, "request_join_remote", _fake)
    r = await client.post(
        "/api/public_spaces/sp-global/join-request",
        json={"host_instance_id": "not-paired"},
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ─── §CP.F1 — minor discovery filter ────────────────────────────────────


async def _seed_with_age(
    client, *, space_id: str, min_age: int, category: str = "general"
):
    repo = SqlitePublicSpaceRepo(client._db)
    await repo.upsert(
        PublicSpaceListing(
            space_id=space_id,
            instance_id="remote-1",
            name=f"Space {space_id}",
            min_age=min_age,
            category=category,
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
    assert adult["category"] == "general"

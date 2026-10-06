"""Tests for the GFS highlight-publication repos + registry.

Covers the publication / token repos and the
:class:`HighlightPublicationRegistry` orchestrator (publish, mint extra
tokens, revoke, unpublish, resolve, author-online check).
"""

from __future__ import annotations

import pytest

from socialhome.global_server.domain import GfsHighlightPublication, GfsHighlightToken
from socialhome.global_server.repositories import (
    SqliteGfsHighlightPublicationRepo,
    SqliteGfsHighlightTokenRepo,
)
from socialhome.global_server.highlight_publications import HighlightPublicationRegistry
from socialhome.global_server.ws_registry import GfsWebSocketRegistry


async def _seed_instance(gfs_db, instance_id: str = "inst-author") -> None:
    await gfs_db.enqueue(
        "INSERT INTO client_instances(instance_id, public_key, status) "
        "VALUES(?, ?, 'active')",
        (instance_id, "deadbeef" * 8),
    )


@pytest.fixture
async def repos(gfs_db):
    await _seed_instance(gfs_db)
    return {
        "pubs": SqliteGfsHighlightPublicationRepo(gfs_db),
        "tokens": SqliteGfsHighlightTokenRepo(gfs_db),
    }


@pytest.fixture
def ws_registry():
    return GfsWebSocketRegistry()


@pytest.fixture
def registry(repos, ws_registry):
    return HighlightPublicationRegistry(
        repos["pubs"],
        repos["tokens"],
        ws_registry,
        base_url="https://gfs.example",
    )


# ── Repos ────────────────────────────────────────────────────────────────


async def test_publication_upsert_and_get(repos):
    pub = GfsHighlightPublication(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=1_000_000,
        published_at=900_000,
        publish_signature="sig",
    )
    await repos["pubs"].upsert(pub)
    got = await repos["pubs"].get("s-1", "inst-author")
    assert got is not None
    assert got.highlight_id == "s-1"
    assert got.expires_at == 1_000_000


async def test_publication_upsert_is_idempotent(repos):
    pub = GfsHighlightPublication(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=1_000_000,
        published_at=900_000,
        publish_signature="sig",
    )
    await repos["pubs"].upsert(pub)
    bumped = GfsHighlightPublication(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=2_000_000,
        published_at=950_000,
        publish_signature="sig2",
    )
    await repos["pubs"].upsert(bumped)
    got = await repos["pubs"].get("s-1", "inst-author")
    assert got is not None
    assert got.expires_at == 2_000_000
    assert got.publish_signature == "sig2"


async def test_publication_delete_returns_rowcount(repos):
    pub = GfsHighlightPublication(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=1_000_000,
        published_at=900_000,
        publish_signature="sig",
    )
    await repos["pubs"].upsert(pub)
    assert await repos["pubs"].delete("s-1", "inst-author") == 1
    # Second delete is a no-op.
    assert await repos["pubs"].delete("s-1", "inst-author") == 0


async def test_publication_prune_expired(repos):
    fresh = GfsHighlightPublication(
        highlight_id="s-fresh",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        published_at=900_000,
        publish_signature="sig",
    )
    expired = GfsHighlightPublication(
        highlight_id="s-exp",
        instance_id="inst-author",
        expires_at=10,  # already past
        published_at=5,
        publish_signature="sig",
    )
    await repos["pubs"].upsert(fresh)
    await repos["pubs"].upsert(expired)
    assert await repos["pubs"].prune_expired(now=1_000_000) == 1
    assert await repos["pubs"].get("s-fresh", "inst-author") is not None
    assert await repos["pubs"].get("s-exp", "inst-author") is None


async def test_token_lookup_returns_none_when_revoked(repos):
    pub = GfsHighlightPublication(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        published_at=900_000,
        publish_signature="sig",
    )
    await repos["pubs"].upsert(pub)
    tok = GfsHighlightToken(
        token="tok-A",
        highlight_id="s-1",
        instance_id="inst-author",
        label="twitter",
        created_at=1_000,
        revoked_at=None,
    )
    await repos["tokens"].insert(tok)
    hit = await repos["tokens"].lookup_active("tok-A", now=2_000)
    assert hit is not None
    assert hit[0].token == "tok-A"
    assert hit[1].highlight_id == "s-1"

    # Revoke and re-check.
    assert await repos["tokens"].revoke("tok-A", "inst-author", now=3_000) == 1
    assert await repos["tokens"].lookup_active("tok-A", now=4_000) is None


async def test_token_lookup_drops_when_publication_expired(repos):
    pub = GfsHighlightPublication(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=2_000,
        published_at=900,
        publish_signature="sig",
    )
    await repos["pubs"].upsert(pub)
    tok = GfsHighlightToken(
        token="tok-B",
        highlight_id="s-1",
        instance_id="inst-author",
        label=None,
        created_at=1_000,
        revoked_at=None,
    )
    await repos["tokens"].insert(tok)
    # ``now`` past publication expires_at — token must lookup as None even
    # though the row is still present and not revoked.
    assert await repos["tokens"].lookup_active("tok-B", now=3_000) is None


async def test_token_revoke_guards_on_instance_id(repos):
    pub = GfsHighlightPublication(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        published_at=900_000,
        publish_signature="sig",
    )
    await repos["pubs"].upsert(pub)
    tok = GfsHighlightToken(
        token="tok-C",
        highlight_id="s-1",
        instance_id="inst-author",
        label=None,
        created_at=1_000,
        revoked_at=None,
    )
    await repos["tokens"].insert(tok)
    # A different instance trying to revoke the same token must fail.
    assert await repos["tokens"].revoke("tok-C", "inst-other", now=1_500) == 0
    # Owning instance succeeds.
    assert await repos["tokens"].revoke("tok-C", "inst-author", now=1_500) == 1


# ── Registry orchestrator ────────────────────────────────────────────────


async def test_record_publish_creates_pub_and_first_token(registry):
    tok, url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="sig",
        label="twitter",
    )
    assert tok.highlight_id == "s-1"
    assert tok.label == "twitter"
    assert url.startswith("https://gfs.example/highlight/inst-author/s-1/")
    assert url.endswith(tok.token)


async def test_mint_extra_token_under_existing_pub(registry):
    await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="sig",
    )
    tok, url = await registry.mint_token(
        highlight_id="s-1",
        instance_id="inst-author",
        label="email",
    )
    assert tok.label == "email"
    assert "/highlight/inst-author/s-1/" in url


async def test_mint_token_without_publish_raises(registry):
    with pytest.raises(LookupError):
        await registry.mint_token(
            highlight_id="s-missing",
            instance_id="inst-author",
            label=None,
        )


async def test_resolve_token_returns_publication(registry):
    tok, _url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="sig",
    )
    resolved = await registry.resolve_token(tok.token)
    assert resolved is not None
    assert resolved.publication.highlight_id == "s-1"
    assert resolved.token.token == tok.token


async def test_revoke_token_makes_resolution_fail(registry):
    tok, _url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="sig",
    )
    revoked = await registry.revoke_token(tok.token, "inst-author")
    assert revoked is True
    assert await registry.resolve_token(tok.token) is None


async def test_remove_publish_cascades_tokens(registry):
    tok, _url = await registry.record_publish(
        highlight_id="s-1",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="sig",
    )
    removed = await registry.remove_publish("s-1", "inst-author")
    assert removed is True
    assert await registry.resolve_token(tok.token) is None


async def test_author_online_reads_ws_registry(registry, ws_registry):
    # Author is offline by default — the WS registry has nothing.
    assert await registry.author_online("inst-author") is False

    # Stub a "live" connection by injecting into the registry's internal
    # store. ``GfsWebSocketRegistry.is_connected`` is a thin set check.
    class _StubWs:
        closed = False

    ws_registry._by_instance["inst-author"] = _StubWs()  # type: ignore[assignment]
    assert await registry.author_online("inst-author") is True

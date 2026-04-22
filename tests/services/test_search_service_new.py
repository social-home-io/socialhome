"""Tests for the §23.2 additions to SearchService.

* People / Spaces scopes
* ``search_with_counts`` shape (hits + per-scope counts)
* 2-char min query
* Access-control filter (space-post hits hidden from non-members)
"""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.search_repo import (
    SCOPE_SPACE,
    SCOPE_USER,
    SqliteSearchRepo,
)
from socialhome.services.search_service import MIN_QUERY_CHARS, SearchService


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    bus = EventBus()
    repo = SqliteSearchRepo(db)
    svc = SearchService(bus, repo)
    svc.wire()
    yield svc, repo
    await db.shutdown()


# ─── People / Spaces scopes ──────────────────────────────────────────


async def test_index_user_and_find_by_username(env):
    svc, _ = env
    await svc.index_user(
        user_id="uid-alice",
        username="alice",
        display_name="Alice Example",
        bio="loves pumpkin bread",
    )
    result = await svc.search_with_counts("pumpkin", type_="people")
    assert len(result["hits"]) == 1
    assert result["hits"][0].scope == SCOPE_USER
    assert result["hits"][0].ref_id == "uid-alice"


async def test_index_space_and_find_by_name(env):
    svc, _ = env
    await svc.index_space(
        space_id="sp-garden",
        name="Garden Club",
        description="weekly tips on composting",
    )
    result = await svc.search_with_counts("composting", type_="spaces")
    assert len(result["hits"]) == 1
    assert result["hits"][0].scope == SCOPE_SPACE
    assert result["hits"][0].ref_id == "sp-garden"


async def test_delete_user_removes_from_index(env):
    svc, _ = env
    await svc.index_user(
        user_id="uid-x", username="x", display_name="X", bio="unique-marker"
    )
    assert len((await svc.search_with_counts("unique-marker"))["hits"]) == 1
    await svc.delete_user("uid-x")
    assert (await svc.search_with_counts("unique-marker"))["hits"] == []


async def test_delete_space_removes_from_index(env):
    svc, _ = env
    await svc.index_space(
        space_id="sp-y",
        name="Y-club",
        description="widgets-galore",
    )
    await svc.delete_space("sp-y")
    assert (await svc.search_with_counts("widgets-galore"))["hits"] == []


async def test_index_user_empty_skips(env):
    svc, _ = env
    await svc.index_user(
        user_id="uid-blank",
        username="",
        display_name="",
        bio=None,
    )
    assert (await svc.search_with_counts("blank"))["hits"] == []


async def test_index_space_empty_skips(env):
    svc, _ = env
    await svc.index_space(space_id="sp-blank", name="", description="")
    assert (await svc.search_with_counts("blank"))["hits"] == []


# ─── Counts ──────────────────────────────────────────────────────────


async def test_search_with_counts_returns_per_scope_counts(env):
    svc, _ = env
    await svc.index_user(user_id="u1", username="lola", display_name="Lola")
    await svc.index_space(
        space_id="sp1",
        name="lola-world",
        description="",
    )
    result = await svc.search_with_counts("lola")
    assert result["counts"] == {"user": 1, "space": 1}


# ─── 2-char min ──────────────────────────────────────────────────────


async def test_search_rejects_under_min_query_length(env):
    svc, _ = env
    await svc.index_user(user_id="u", username="zz", display_name="Zzz")
    # 1-char query → empty.
    assert MIN_QUERY_CHARS == 2
    result = await svc.search_with_counts("z")
    assert result == {"hits": [], "counts": {}}
    # 2+ chars matches.
    assert len((await svc.search_with_counts("zz"))["hits"]) == 1


async def test_search_back_compat_list_shape(env):
    """Legacy ``search`` still returns a plain list for back-compat."""
    svc, _ = env
    await svc.index_user(user_id="u", username="lego", display_name="Lego")
    hits = await svc.search("lego")
    assert isinstance(hits, list)
    assert len(hits) == 1


# ─── Type-group filter (posts=post+space_post) ──────────────────────


async def test_type_posts_expands_to_post_and_space_post(env):
    svc, _ = env
    # Raw repo upserts to avoid constructing full Post domain objects.
    await svc._repo.upsert(
        scope="post",
        ref_id="p1",
        space_id=None,
        title="",
        body="pudding recipe",
    )
    await svc._repo.upsert(
        scope="space_post",
        ref_id="sp1",
        space_id=None,
        title="",
        body="pudding tip",
    )
    await svc.index_user(user_id="u", username="pudding-fan", display_name="Lover")
    result = await svc.search_with_counts("pudding", type_="posts")
    # People hit excluded; both post + space_post included.
    scopes = {h.scope for h in result["hits"]}
    assert scopes == {"post", "space_post"}


# ─── Access-control filter ──────────────────────────────────────────


class _FakeSpaceRepo:
    def __init__(self, member_map: dict[str, list[str]]):
        self._m = member_map

    async def list_local_member_user_ids(self, space_id: str) -> list[str]:
        return self._m.get(space_id, [])


async def test_access_filter_drops_non_member_space_post(env):
    svc, _ = env
    svc.attach_access_repos(
        space_repo=_FakeSpaceRepo({"sp-private": ["uid-owner"]}),
    )
    await svc._repo.upsert(
        scope="space_post",
        ref_id="p1",
        space_id="sp-private",
        title="",
        body="secret recipe",
    )
    # Caller is NOT a member of sp-private → hidden.
    result = await svc.search_with_counts(
        "secret",
        caller_user_id="uid-outsider",
        caller_username="outsider",
    )
    assert result["hits"] == []
    # But raw counts still reveal the hit exists — spec trade-off.
    assert result["counts"] == {"space_post": 1}


async def test_access_filter_lets_member_see_space_post(env):
    svc, _ = env
    svc.attach_access_repos(
        space_repo=_FakeSpaceRepo({"sp-private": ["uid-alice"]}),
    )
    await svc._repo.upsert(
        scope="space_post",
        ref_id="p1",
        space_id="sp-private",
        title="",
        body="secret recipe",
    )
    result = await svc.search_with_counts(
        "secret",
        caller_user_id="uid-alice",
        caller_username="alice",
    )
    assert len(result["hits"]) == 1


async def test_access_filter_drops_dm_hits_when_wired(env):
    """Privacy-preserving default: when access repos are wired, DM hits
    drop (we can't verify conversation membership today)."""
    svc, _ = env
    svc.attach_access_repos(space_repo=_FakeSpaceRepo({}))
    await svc._repo.upsert(
        scope="message",
        ref_id="m1",
        space_id=None,
        title="alice",
        body="secret dm body",
    )
    result = await svc.search_with_counts(
        "secret",
        caller_user_id="uid-x",
        caller_username="x",
    )
    assert result["hits"] == []


async def test_access_filter_no_op_when_unwired(env):
    """Standalone mode (no access repos) keeps existing permissive behaviour."""
    svc, _ = env
    await svc._repo.upsert(
        scope="space_post",
        ref_id="p1",
        space_id="sp-x",
        title="",
        body="content",
    )
    result = await svc.search_with_counts("content")
    assert len(result["hits"]) == 1

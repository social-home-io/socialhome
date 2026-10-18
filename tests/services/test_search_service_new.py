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
    # A space row is tied to its space: found by those with a seat only.
    svc.attach_access_repos(space_repo=_FakeSpaceRepo({"uid-me": ["sp-garden"]}))
    other = await svc.search_with_counts(
        "composting", type_="spaces", caller_user_id="uid-other"
    )
    assert other["hits"] == []
    result = await svc.search_with_counts(
        "composting", type_="spaces", caller_user_id="uid-me"
    )
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
    svc.attach_access_repos(space_repo=_FakeSpaceRepo({"uid-me": ["sp1"]}))
    result = await svc.search_with_counts("lola", caller_user_id="uid-me")
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


# ─── Access control (§23.2.6) — fail closed ─────────────────────────


class _Space:
    def __init__(self, space_id: str) -> None:
        self.id = space_id


class _FakeSpaceRepo:
    """``list_for_user`` from a ``{user_id: [space_id, ...]}`` seat map."""

    def __init__(self, seats: dict[str, list[str]]):
        self._seats = seats

    async def list_for_user(self, user_id: str) -> list[_Space]:
        return [_Space(sid) for sid in self._seats.get(user_id, [])]


class _BrokenSpaceRepo:
    async def list_for_user(self, user_id: str):
        raise RuntimeError("db gone")


async def _secret(svc, space_id: str | None = "sp-private", scope: str = "space_post"):
    await svc._repo.upsert(
        scope=scope,
        ref_id=f"{scope}-1",
        space_id=space_id,
        title="",
        body="secret recipe",
    )


async def test_access_filter_drops_non_member_space_post(env):
    """No seat → neither the hit nor its count."""
    svc, _ = env
    svc.attach_access_repos(space_repo=_FakeSpaceRepo({"uid-owner": ["sp-private"]}))
    await _secret(svc)
    result = await svc.search_with_counts("secret", caller_user_id="uid-outsider")
    assert result == {"hits": [], "counts": {}}


async def test_access_filter_lets_member_see_space_post(env):
    svc, _ = env
    svc.attach_access_repos(space_repo=_FakeSpaceRepo({"uid-alice": ["sp-private"]}))
    await _secret(svc)
    result = await svc.search_with_counts("secret", caller_user_id="uid-alice")
    assert len(result["hits"]) == 1
    assert result["counts"] == {"space_post": 1}


async def test_a_space_id_without_a_seat_answers_like_no_match(env):
    svc, _ = env
    svc.attach_access_repos(space_repo=_FakeSpaceRepo({"uid-owner": ["sp-private"]}))
    await _secret(svc)
    result = await svc.search_with_counts(
        "secret", space_id="sp-private", caller_user_id="uid-outsider"
    )
    assert result == {"hits": [], "counts": {}}


async def test_access_filter_drops_dm_hits_and_counts(env):
    """A DM row carries no conversation id — never shown, never counted."""
    svc, _ = env
    svc.attach_access_repos(space_repo=_FakeSpaceRepo({}))
    await _secret(svc, space_id=None, scope="message")
    result = await svc.search_with_counts("secret", caller_user_id="uid-x")
    assert result == {"hits": [], "counts": {}}


async def test_access_filter_fails_closed_when_unwired(env):
    """No space repo → no seats → nothing space-bound (was: everything)."""
    svc, _ = env
    await _secret(svc, space_id="sp-x")
    await _secret(svc, space_id=None, scope="post")
    result = await svc.search_with_counts("secret", caller_user_id="uid-x")
    assert [h.scope for h in result["hits"]] == ["post"]
    assert result["counts"] == {"post": 1}


async def test_access_filter_fails_closed_without_a_caller(env):
    svc, _ = env
    svc.attach_access_repos(space_repo=_FakeSpaceRepo({"uid-x": ["sp-x"]}))
    await _secret(svc, space_id="sp-x")
    assert await svc.search_with_counts("secret") == {"hits": [], "counts": {}}


async def test_a_roster_error_fails_the_search_never_widens_it(env):
    """The old filter let every hit of a space through when reading its
    roster raised — now the error propagates."""
    svc, _ = env
    svc.attach_access_repos(space_repo=_BrokenSpaceRepo())
    await _secret(svc)
    with pytest.raises(RuntimeError):
        await svc.search_with_counts("secret", caller_user_id="uid-x")

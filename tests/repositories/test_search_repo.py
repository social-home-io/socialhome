"""Tests for SqliteSearchRepo (FTS5)."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.repositories.search_repo import (
    SCOPE_POST,
    SCOPE_SPACE_POST,
    SqliteSearchRepo,
)


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    yield SqliteSearchRepo(db)
    await db.shutdown()


# ─── upsert + delete ─────────────────────────────────────────────────────


async def test_upsert_then_search_finds_match(repo):
    await repo.upsert(
        scope=SCOPE_POST,
        ref_id="p1",
        space_id=None,
        title="",
        body="hello world from pascal",
    )
    hits = await repo.search("hello")
    assert len(hits) == 1
    assert hits[0].ref_id == "p1"


async def test_upsert_replaces_existing_row(repo):
    await repo.upsert(
        scope=SCOPE_POST, ref_id="p1", space_id=None, title="", body="version one"
    )
    await repo.upsert(
        scope=SCOPE_POST, ref_id="p1", space_id=None, title="", body="version two"
    )
    hits_v1 = await repo.search("version one")
    hits_v2 = await repo.search("version two")
    assert hits_v1 == []  # old version replaced
    assert len(hits_v2) == 1


async def test_delete_drops_row(repo):
    await repo.upsert(
        scope=SCOPE_POST, ref_id="p1", space_id=None, title="", body="please delete me"
    )
    await repo.delete(scope=SCOPE_POST, ref_id="p1")
    assert await repo.search("delete") == []


async def test_upsert_rejects_unknown_scope(repo):
    with pytest.raises(ValueError):
        await repo.upsert(scope="snoop", ref_id="x", space_id=None, title="", body="x")


# ─── search semantics ────────────────────────────────────────────────────


async def test_search_empty_query_returns_no_hits(repo):
    assert await repo.search("") == []
    assert await repo.search("   ") == []


async def test_search_treats_input_as_phrase_no_operator_injection(repo):
    """User input must not be interpreted as FTS5 operators."""
    await repo.upsert(
        scope=SCOPE_POST, ref_id="p1", space_id=None, title="", body="alpha beta gamma"
    )
    # FTS5 NOT operator would normally drop docs containing "beta"; we treat it as phrase.
    hits = await repo.search("alpha NOT beta")
    assert hits == []  # phrase doesn't match anything


async def test_search_filters_by_scope(repo):
    await repo.upsert(
        scope=SCOPE_POST, ref_id="p1", space_id=None, title="", body="needle"
    )
    await repo.upsert(
        scope=SCOPE_SPACE_POST, ref_id="sp1", space_id="sp-A", title="", body="needle"
    )
    hits_post = await repo.search("needle", scopes=frozenset({SCOPE_POST}))
    assert {h.ref_id for h in hits_post} == {"p1"}


async def test_search_filters_by_space_id(repo):
    await repo.upsert(
        scope=SCOPE_SPACE_POST, ref_id="s1", space_id="A", title="", body="findme"
    )
    await repo.upsert(
        scope=SCOPE_SPACE_POST, ref_id="s2", space_id="B", title="", body="findme"
    )
    hits = await repo.search("findme", space_id="A")
    assert {h.ref_id for h in hits} == {"s1"}


async def test_search_limit_clamps_to_100(repo):
    for i in range(120):
        await repo.upsert(
            scope=SCOPE_POST,
            ref_id=f"p{i}",
            space_id=None,
            title="",
            body=f"common term row{i}",
        )
    hits = await repo.search("common", limit=10000)
    assert len(hits) <= 100


async def test_search_returns_snippet_with_highlight(repo):
    await repo.upsert(
        scope=SCOPE_POST,
        ref_id="p1",
        space_id=None,
        title="",
        body="The quick brown fox jumps over the lazy dog.",
    )
    hits = await repo.search("brown")
    assert len(hits) == 1
    assert "<mark>" in hits[0].snippet
    assert "brown" in hits[0].snippet.lower()


async def test_search_diacritic_folding(repo):
    """unicode61 with remove_diacritics=2 means café == cafe."""
    await repo.upsert(
        scope=SCOPE_POST,
        ref_id="p1",
        space_id=None,
        title="",
        body="meet me at the café tonight",
    )
    hits = await repo.search("cafe")
    assert len(hits) == 1


async def _rows(repo) -> None:
    for scope, ref, space in (
        ("post", "a", None),
        ("space_post", "b", "sp-1"),
        ("space_post", "c", "sp-2"),
        ("message", "d", None),
    ):
        await repo.upsert(
            scope=scope, ref_id=ref, space_id=space, title="", body="kiwi"
        )


async def test_visible_space_ids_and_excluded_scopes_filter_in_sql(repo):
    """Rows tied to a space outside ``visible_space_ids`` and excluded
    scopes are neither returned nor counted — the page is over what the
    caller may see."""
    await _rows(repo)
    hits = await repo.search(
        "kiwi", visible_space_ids={"sp-1"}, exclude_scopes={"message"}
    )
    assert {h.ref_id for h in hits} == {"a", "b"}
    counts = await repo.count_by_scope(
        "kiwi", visible_space_ids={"sp-1"}, exclude_scopes={"message"}
    )
    assert counts == {"post": 1, "space_post": 1}


async def test_no_visible_spaces_keeps_only_unscoped_rows(repo):
    await _rows(repo)
    hits = await repo.search("kiwi", visible_space_ids=())
    assert {h.ref_id for h in hits} == {"a", "d"}
    assert await repo.count_by_scope("kiwi", visible_space_ids=()) == {
        "post": 1,
        "message": 1,
    }


async def test_no_visibility_filter_is_unrestricted(repo):
    await _rows(repo)
    assert len(await repo.search("kiwi")) == 4

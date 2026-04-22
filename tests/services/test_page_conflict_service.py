"""Tests for §4.4.4.1 page three-way merge + conflict resolution."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.repositories.page_repo import (
    PageNotFoundError,
    SqlitePageRepo,
    new_page,
)
from socialhome.services.page_conflict_service import (
    CONFLICT_HEAD,
    CONFLICT_SEP,
    CONFLICT_TAIL,
    NoActiveConflictError,
    PageConflictService,
    diff3_merge,
)


# ─── diff3 primitive ─────────────────────────────────────────────────────


def test_diff3_all_equal_passes_through():
    r = diff3_merge("a\n\nb", "a\n\nb", "a\n\nb")
    assert not r.has_conflict
    assert r.content == "a\n\nb"


def test_diff3_only_mine_changed_takes_mine():
    r = diff3_merge("p1\n\np2", "p1-new\n\np2", "p1\n\np2")
    assert not r.has_conflict
    assert r.content == "p1-new\n\np2"


def test_diff3_only_theirs_changed_takes_theirs():
    r = diff3_merge("p1\n\np2", "p1\n\np2", "p1\n\np2-new")
    assert not r.has_conflict
    assert r.content == "p1\n\np2-new"


def test_diff3_parallel_identical_edits_collapse():
    r = diff3_merge("p1", "p1-new", "p1-new")
    assert not r.has_conflict
    assert r.content == "p1-new"


def test_diff3_parallel_disagreeing_edits_conflict():
    r = diff3_merge("p1", "p1-mine", "p1-theirs")
    assert r.has_conflict
    assert CONFLICT_HEAD in r.content
    assert "p1-mine" in r.content
    assert CONFLICT_SEP in r.content
    assert "p1-theirs" in r.content
    assert CONFLICT_TAIL in r.content


def test_diff3_deletion_one_side_wins_over_unchanged_other():
    r = diff3_merge("keep\n\ndrop", "keep", "keep\n\ndrop")
    assert not r.has_conflict
    assert r.content == "keep"


def test_diff3_deletion_vs_edit_is_conflict():
    r = diff3_merge("keep\n\ndrop", "keep", "keep\n\ndrop-edited")
    assert r.has_conflict


def test_diff3_crlf_normalisation():
    r = diff3_merge("a\r\n\r\nb", "a\r\n\r\nb", "a\r\n\r\nb")
    assert not r.has_conflict


def test_diff3_empty_inputs():
    r = diff3_merge("", "", "")
    assert not r.has_conflict
    assert r.content == ""


# ─── PageConflictService ─────────────────────────────────────────────────


@pytest.fixture
async def svc(tmp_dir):
    db = AsyncDatabase(tmp_dir / "pc.db", batch_timeout_ms=10)
    await db.startup()
    # Need a space_pages row. Seed a space first.
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES('sp-1', 'test', 'iid', 'u1', ?, 'household')",
        ("aa" * 32,),
    )
    repo = SqlitePageRepo(db)
    page = new_page(
        title="t",
        content="original",
        created_by="u1",
        space_id="sp-1",
    )
    await repo.save(page)
    svc = PageConflictService(repo)
    yield db, repo, svc, page
    await db.shutdown()


async def test_merge_remote_clean_applies_silently(svc):
    db, repo, service, page = svc
    await service.record_base(
        page_id=page.id,
        space_id="sp-1",
        body="original",
        author_user_id="u1",
    )
    # Mine == base; theirs has a new paragraph appended.
    result = await service.merge_remote_body(
        page_id=page.id,
        space_id="sp-1",
        remote_body="original\n\nadded",
        remote_author_user_id="u2",
    )
    assert not result.has_conflict
    current = await repo.get(page.id)
    assert "added" in current.content


async def test_merge_remote_conflict_stores_both_sides(svc):
    db, repo, service, page = svc
    await service.record_base(
        page_id=page.id,
        space_id="sp-1",
        body="original",
        author_user_id="u1",
    )
    # Diverge locally.
    from dataclasses import replace

    await repo.save(replace(page, content="mine-version"))
    # Remote also diverged from base — conflict.
    result = await service.merge_remote_body(
        page_id=page.id,
        space_id="sp-1",
        remote_body="theirs-version",
        remote_author_user_id="u2",
    )
    assert result.has_conflict
    assert await service.has_active_conflict(page.id)


async def test_merge_remote_page_missing_raises(svc):
    _, _, service, _ = svc
    with pytest.raises(PageNotFoundError):
        await service.merge_remote_body(
            page_id="nope",
            space_id="sp-1",
            remote_body="x",
            remote_author_user_id="u2",
        )


async def test_resolve_conflict_mine_keeps_local(svc):
    db, repo, service, page = svc
    from dataclasses import replace

    await service.record_base(
        page_id=page.id,
        space_id="sp-1",
        body="original",
        author_user_id="u1",
    )
    await repo.save(replace(page, content="mine-version"))
    await service.merge_remote_body(
        page_id=page.id,
        space_id="sp-1",
        remote_body="theirs-version",
        remote_author_user_id="u2",
    )
    out = await service.resolve_conflict(
        space_id="sp-1",
        page_id=page.id,
        user_id="u1",
        resolution="mine",
    )
    assert out == "mine-version"
    assert not await service.has_active_conflict(page.id)


async def test_resolve_conflict_theirs_applies_remote(svc):
    db, repo, service, page = svc
    from dataclasses import replace

    await service.record_base(
        page_id=page.id,
        space_id="sp-1",
        body="original",
        author_user_id="u1",
    )
    await repo.save(replace(page, content="mine-version"))
    await service.merge_remote_body(
        page_id=page.id,
        space_id="sp-1",
        remote_body="theirs-version",
        remote_author_user_id="u2",
    )
    out = await service.resolve_conflict(
        space_id="sp-1",
        page_id=page.id,
        user_id="u1",
        resolution="theirs",
    )
    assert out == "theirs-version"
    current = await repo.get(page.id)
    assert current.content == "theirs-version"


async def test_resolve_conflict_merged_requires_content(svc):
    db, repo, service, page = svc
    from dataclasses import replace

    await service.record_base(
        page_id=page.id,
        space_id="sp-1",
        body="original",
        author_user_id="u1",
    )
    await repo.save(replace(page, content="mine-version"))
    await service.merge_remote_body(
        page_id=page.id,
        space_id="sp-1",
        remote_body="theirs-version",
        remote_author_user_id="u2",
    )
    with pytest.raises(ValueError):
        await service.resolve_conflict(
            space_id="sp-1",
            page_id=page.id,
            user_id="u1",
            resolution="merged_content",
            merged_content=None,
        )


async def test_resolve_conflict_merged_applies_provided_body(svc):
    db, repo, service, page = svc
    from dataclasses import replace

    await service.record_base(
        page_id=page.id,
        space_id="sp-1",
        body="original",
        author_user_id="u1",
    )
    await repo.save(replace(page, content="mine-version"))
    await service.merge_remote_body(
        page_id=page.id,
        space_id="sp-1",
        remote_body="theirs-version",
        remote_author_user_id="u2",
    )
    out = await service.resolve_conflict(
        space_id="sp-1",
        page_id=page.id,
        user_id="u1",
        resolution="merged_content",
        merged_content="hand-merged",
    )
    assert out == "hand-merged"
    assert not await service.has_active_conflict(page.id)


async def test_resolve_unknown_resolution_raises(svc):
    _, _, service, page = svc
    with pytest.raises(ValueError):
        await service.resolve_conflict(
            space_id="sp-1",
            page_id=page.id,
            user_id="u1",
            resolution="invalid",
        )


async def test_resolve_without_active_conflict_raises(svc):
    _, _, service, page = svc
    with pytest.raises(NoActiveConflictError):
        await service.resolve_conflict(
            space_id="sp-1",
            page_id=page.id,
            user_id="u1",
            resolution="mine",
        )


async def test_resolve_missing_page_raises(svc):
    db, _, service, _ = svc
    # Manually insert a conflict row for a page that doesn't exist.
    await db.enqueue(
        "INSERT INTO space_page_snapshots(page_id, space_id, body, "
        "snapshot_by, side, conflict) VALUES('ghost','sp-1','x','u','mine',1)",
    )
    with pytest.raises(PageNotFoundError):
        await service.resolve_conflict(
            space_id="sp-1",
            page_id="ghost",
            user_id="u1",
            resolution="mine",
        )

"""Repository-level tests for the highlights tables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.highlight import (
    Highlight,
    HighlightAudience,
    HighlightFrame,
    HighlightFrameType,
)
from socialhome.repositories.highlight_repo import SqliteHighlightRepo


async def _seed_user(db, user_id: str = "u1", username: str = "pascal") -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES(?,?,?, 'active')",
        (user_id, username, username.title()),
    )


@pytest.fixture
async def repo(db):
    await db.enqueue("INSERT OR IGNORE INTO preferences(id) VALUES('household')")
    return SqliteHighlightRepo(db)


def _expires(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


async def test_find_or_create_today_is_idempotent(db, repo):
    await _seed_user(db)
    highlight1 = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    highlight2 = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    assert highlight1.id == highlight2.id


async def test_append_frame_assigns_sequential_numbers(db, repo):
    await _seed_user(db)
    highlight = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    f1 = await repo.append_frame(
        highlight_id=highlight.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    f2 = await repo.append_frame(
        highlight_id=highlight.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/b.webp",
    )
    assert f1.sequence == 1
    assert f2.sequence == 2
    frames = await repo.list_frames(highlight.id)
    assert [f.sequence for f in frames] == [1, 2]


async def test_view_and_reaction_round_trip(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    highlight = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    frame = await repo.append_frame(
        highlight_id=highlight.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    await repo.mark_viewed(frame.id, "u2")
    views = await repo.list_views_for_frame(frame.id)
    assert len(views) == 1 and views[0].viewer_user_id == "u2"

    await repo.set_reaction(frame.id, "u2", "🔥")
    rs = await repo.list_reactions_for_frame(frame.id)
    assert len(rs) == 1 and rs[0].emoji == "🔥"
    # Changing the emoji upserts (still one row).
    await repo.set_reaction(frame.id, "u2", "❤️")
    rs = await repo.list_reactions_for_frame(frame.id)
    assert len(rs) == 1 and rs[0].emoji == "❤️"


async def test_prune_expired_drops_only_old_rows(db, repo):
    await _seed_user(db)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = _expires(7)
    expired = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-04-01",
        expires_at=past,
    )
    fresh = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=future,
    )
    pruned = await repo.prune_expired()
    assert pruned == 1
    assert await repo.get_highlight(expired.id) is None
    assert await repo.get_highlight(fresh.id) is not None


async def test_visibility_filters_users_audience(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    await _seed_user(db, "u3", "lina")
    # USERS-kind highlight aimed at u2 only.
    await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.USERS,
        audience=("u2",),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    seen_by_u2 = await repo.list_visible_to("u2")
    seen_by_u3 = await repo.list_visible_to("u3")
    seen_by_u1 = await repo.list_visible_to("u1")  # author always sees own
    assert len(seen_by_u2) == 1
    assert len(seen_by_u3) == 0
    assert len(seen_by_u1) == 1


async def test_visibility_excludes_blocked_authors(db, repo):
    """list_visible_to skips highlights from authors the viewer has blocked."""
    await _seed_user(db, "u1", "pascal")  # author
    await _seed_user(db, "u2", "maria")  # viewer
    await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    # Without a block u2 sees the highlight.
    assert len(await repo.list_visible_to("u2")) == 1
    # After blocking u1, the highlight is hidden from u2's inbox.
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("u2", "u1"),
    )
    assert await repo.list_visible_to("u2") == []
    # The author still sees their own highlight — block doesn't gag yourself.
    assert len(await repo.list_visible_to("u1")) == 1


async def test_count_unseen_frames_drops_to_zero_after_view(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    highlight = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    f1 = await repo.append_frame(
        highlight_id=highlight.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    f2 = await repo.append_frame(
        highlight_id=highlight.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/b.webp",
    )
    assert await repo.count_unseen_frames(highlight.id, "u2") == 2
    await repo.mark_viewed(f1.id, "u2")
    assert await repo.count_unseen_frames(highlight.id, "u2") == 1
    await repo.mark_viewed(f2.id, "u2")
    assert await repo.count_unseen_frames(highlight.id, "u2") == 0


async def test_save_highlight_and_save_frame_upsert(db, repo):
    """Federation upsert path: caller-supplied id is preserved."""
    await _seed_user(db)
    s = Highlight(
        id="highlight-remote-1",
        author_user_id="u1",
        highlight_date="2026-05-03",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        created_at="2026-05-03T08:00:00+00:00",
        expires_at=_expires(),
    )
    out = await repo.save_highlight(s)
    assert out.id == "highlight-remote-1"
    assert (await repo.get_highlight("highlight-remote-1")) is not None
    f = HighlightFrame(
        id="frame-remote-1",
        highlight_id="highlight-remote-1",
        sequence=1,
        frame_type=HighlightFrameType.VIDEO,
        media_url="/api/media/v.mp4",
        duration_ms=4500,
    )
    await repo.save_frame(f)
    fetched = await repo.get_frame("frame-remote-1")
    assert fetched is not None
    assert fetched.frame_type is HighlightFrameType.VIDEO
    assert fetched.duration_ms == 4500


async def test_clear_reaction_and_delete_frame(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    highlight = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    frame = await repo.append_frame(
        highlight_id=highlight.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/a.webp",
    )
    await repo.set_reaction(frame.id, "u2", "🔥")
    await repo.clear_reaction(frame.id, "u2")
    assert await repo.list_reactions_for_frame(frame.id) == []
    await repo.delete_frame(frame.id)
    assert await repo.get_frame(frame.id) is None


async def test_prune_over_max_drops_oldest(db, repo):
    await _seed_user(db)
    # Three highlights on different days; max_count=1 keeps the newest.
    for d in ("2026-05-01", "2026-05-02", "2026-05-03"):
        await repo.find_or_create_today(
            author_user_id="u1",
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
            highlight_date=d,
            expires_at=_expires(),
        )
    pruned = await repo.prune_over_max("u1", max_count=1)
    assert pruned == 2
    rest = await repo.list_authored("u1")
    assert len(rest) == 1
    assert rest[0].highlight_date == "2026-05-03"


async def test_list_authors_with_highlights(db, repo):
    await _seed_user(db, "u1", "pascal")
    await _seed_user(db, "u2", "maria")
    for uid in ("u1", "u2", "u1"):  # u1 twice → still listed once
        await repo.find_or_create_today(
            author_user_id=uid,
            audience_kind=HighlightAudience.ALL_PAIRED,
            audience=(),
            highlight_date={"u1": "2026-05-03", "u2": "2026-05-04"}[uid],
            expires_at=_expires(),
        )
    authors = sorted(await repo.list_authors_with_highlights())
    assert authors == ["u1", "u2"]


# ── Public publication state (§highlights_public) ───────────────────────────


async def _seed_gfs_connection(db, gfs_id: str = "gfs-abc") -> None:
    await db.enqueue(
        """
        INSERT INTO gfs_connections(
            id, gfs_instance_id, display_name, public_key, inbox_url,
            status, paired_at
        ) VALUES(?,?,?,?,?,?,datetime('now'))
        """,
        (gfs_id, gfs_id, gfs_id, "ff" * 32, f"https://{gfs_id}.example", "active"),
    )


async def test_mark_published_sets_flag_and_persists(db, repo):
    await _seed_user(db)
    await _seed_gfs_connection(db)
    highlight = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    await repo.mark_published(
        highlight.id,
        gfs_id="gfs-abc",
        published_at="2026-05-03T12:00:00+00:00",
    )
    refreshed = await repo.get_highlight(highlight.id)
    assert refreshed is not None
    assert refreshed.public_gfs_id == "gfs-abc"
    assert refreshed.public_published_at == "2026-05-03T12:00:00+00:00"


async def test_mark_unpublished_clears_flag(db, repo):
    await _seed_user(db)
    await _seed_gfs_connection(db)
    highlight = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    await repo.mark_published(
        highlight.id,
        gfs_id="gfs-abc",
        published_at="2026-05-03T12:00:00+00:00",
    )
    await repo.mark_unpublished(highlight.id)
    refreshed = await repo.get_highlight(highlight.id)
    assert refreshed is not None
    assert refreshed.public_gfs_id is None
    assert refreshed.public_published_at is None


async def test_list_published_for_filters_to_author_and_published(db, repo):
    await _seed_user(db, user_id="u1", username="alice")
    await _seed_user(db, user_id="u2", username="bob")
    await _seed_gfs_connection(db, gfs_id="gfs-abc")
    await _seed_gfs_connection(db, gfs_id="gfs-xyz")
    s1 = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    await repo.find_or_create_today(  # u1 unpublished — should be skipped
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-04",
        expires_at=_expires(),
    )
    s3 = await repo.find_or_create_today(  # u2 published — different author
        author_user_id="u2",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    await repo.mark_published(
        s1.id,
        gfs_id="gfs-abc",
        published_at="2026-05-03T10:00:00+00:00",
    )
    await repo.mark_published(
        s3.id,
        gfs_id="gfs-xyz",
        published_at="2026-05-03T11:00:00+00:00",
    )

    out = await repo.list_published_for("u1")
    assert [s.id for s in out] == [s1.id]


def _expired_today_iso() -> str:
    """A tz-aware expiry at the very start of the current UTC day.

    Always in the past, and always on the *same calendar date* as SQLite's
    ``datetime('now')`` — the only window where a raw TEXT compare went
    wrong ("T" 0x54 outranks " " 0x20 only once the date digits tie).
    """
    return f"{datetime.now(timezone.utc).date().isoformat()}T00:00:00.000001+00:00"


async def _dated_highlight(repo, *, date: str, expires_at: str):
    return await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date=date,
        expires_at=expires_at,
    )


async def test_highlight_expired_today_is_not_visible(db, repo):
    """A highlight that expired earlier today is already hidden.

    Regression: ``highlights.expires_at`` holds tz-aware ISO 8601 and was
    compared as raw TEXT against SQLite's naive ``datetime('now')``, so an
    expired highlight stayed visible until the UTC date rolled over.
    """
    await _seed_user(db)
    gone = await _dated_highlight(
        repo, date="2026-04-01", expires_at=_expired_today_iso()
    )
    live = await _dated_highlight(repo, date="2026-05-03", expires_at=_expires(7))
    ids = {h.id for h in await repo.list_visible_to("u1")}
    assert gone.id not in ids
    assert live.id in ids


async def test_highlight_expired_today_is_pruned(db, repo):
    """The retention sweep reclaims a highlight that expired earlier today."""
    await _seed_user(db)
    gone = await _dated_highlight(
        repo, date="2026-04-01", expires_at=_expired_today_iso()
    )
    live = await _dated_highlight(repo, date="2026-05-03", expires_at=_expires(7))
    assert await repo.prune_expired() == 1
    assert await repo.get_highlight(gone.id) is None
    assert await repo.get_highlight(live.id) is not None

"""Tests for SqliteSpacePostRepo — space feed posts, reactions, comments."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with a space post repo and a seeded space + user."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, identity_public_key)"
        " VALUES(?,?,?,?,?)",
        ("sp-1", "TestSpace", "inst-x", "alice", "aabb" * 16),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteSpacePostRepo(db)
    e.space_id = "sp-1"
    yield e
    await db.shutdown()


def _post(post_id: str, author: str = "uid-alice") -> Post:
    return Post(
        id=post_id,
        author=author,
        type=PostType.TEXT,
        created_at=datetime.now(timezone.utc),
        content="Hello space!",
    )


def _comment(comment_id: str, post_id: str, author: str = "uid-alice") -> Comment:
    return Comment(
        id=comment_id,
        post_id=post_id,
        author=author,
        type=CommentType.TEXT,
        created_at=datetime.now(timezone.utc),
        content="Great post!",
    )


# ── Posts ─────────────────────────────────────────────────────────────────


async def test_save_and_get_post(env):
    """save persists a space post; get retrieves (space_id, post)."""
    post = _post("sp-p1")
    await env.repo.save(env.space_id, post)
    result = await env.repo.get("sp-p1")
    assert result is not None
    sid, fetched = result
    assert sid == env.space_id
    assert fetched.content == "Hello space!"


async def test_get_missing_post(env):
    """get returns None for an unknown post id."""
    assert await env.repo.get("nope") is None


async def test_list_feed_scoped_to_space(env):
    """list_feed returns only posts for the given space_id."""
    await env.repo.save(env.space_id, _post("sp-lf1"))
    await env.repo.save(env.space_id, _post("sp-lf2"))
    results = await env.repo.list_feed(env.space_id)
    assert len(results) == 2


async def test_list_feed_excludes_hidden_from_feed(env):
    """list_feed drops posts flagged hidden_from_feed (Bazaar/calendar
    anchor posts the author didn't announce) but still persists them."""
    from dataclasses import replace

    visible = _post("sp-vis")
    hidden = replace(_post("sp-hid"), hidden_from_feed=True)
    await env.repo.save(env.space_id, visible)
    await env.repo.save(env.space_id, hidden)
    feed_ids = {p.id for p in await env.repo.list_feed(env.space_id)}
    assert "sp-vis" in feed_ids
    assert "sp-hid" not in feed_ids
    # The hidden post still exists and round-trips its flag (the Bazaar
    # tab reads it by id regardless of feed visibility).
    got = await env.repo.get("sp-hid")
    assert got is not None
    assert got[1].hidden_from_feed is True


async def test_list_for_sync_ships_hidden_anchors_but_not_deleted_posts(env):
    """The §25.6 catch-up exporters enumerate through ``list_for_sync``.

    A bazaar listing or calendar event the author did not announce hangs
    on a ``hidden_from_feed`` anchor post; ``bazaar_listings.post_id``
    references it. ``list_feed`` rightly hides that anchor from readers,
    but a joiner that never receives it cannot store the listing — the
    INSERT fails its FK and the listing silently never arrives. So the
    sync query includes every non-deleted post, flag intact, and still
    skips soft-deleted rows (the provider never back-emits deletes).
    """
    from dataclasses import replace

    visible = _post("sp-sync-vis")
    hidden = replace(_post("sp-sync-anchor"), hidden_from_feed=True)
    gone = _post("sp-sync-gone")
    for p in (visible, hidden, gone):
        await env.repo.save(env.space_id, p)
    await env.repo.soft_delete("sp-sync-gone")

    synced = {p.id: p for p in await env.repo.list_for_sync(env.space_id)}
    assert set(synced) == {"sp-sync-vis", "sp-sync-anchor"}
    assert synced["sp-sync-anchor"].hidden_from_feed is True
    # And the feed contract is unchanged: the anchor stays out of it.
    assert "sp-sync-anchor" not in {
        p.id for p in await env.repo.list_feed(env.space_id)
    }


async def test_list_feed_excludes_deleted(env):
    """list_feed does not return soft-deleted posts."""
    post = _post("sp-del-1")
    await env.repo.save(env.space_id, post)
    await env.repo.soft_delete("sp-del-1")
    results = await env.repo.list_feed(env.space_id)
    assert not any(p.id == "sp-del-1" for p in results)


async def test_list_since_returns_strictly_newer_posts(env):
    """``list_since`` is exclusive on ``since`` and ASC-ordered by created_at."""
    older = Post(
        id="sp-old",
        author="uid-alice",
        type=PostType.TEXT,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        content="old",
    )
    newer = Post(
        id="sp-new",
        author="uid-alice",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 1, 12, tzinfo=timezone.utc),
        content="new",
    )
    await env.repo.save(env.space_id, older)
    await env.repo.save(env.space_id, newer)
    cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc).isoformat()
    results = await env.repo.list_since(env.space_id, cutoff)
    assert [p.id for p in results] == ["sp-new"]


async def test_list_since_excludes_deleted(env):
    """Soft-deleted posts are not replayed on resume."""
    post = _post("sp-deleted")
    await env.repo.save(env.space_id, post)
    await env.repo.soft_delete("sp-deleted")
    results = await env.repo.list_since(env.space_id, "2020-01-01T00:00:00+00:00")
    assert not any(p.id == "sp-deleted" for p in results)


async def test_list_since_respects_limit(env):
    """``limit`` caps the burst size to bound a single resume."""
    for i in range(5):
        await env.repo.save(env.space_id, _post(f"sp-l-{i}"))
    results = await env.repo.list_since(
        env.space_id,
        "2020-01-01T00:00:00+00:00",
        limit=2,
    )
    assert len(results) == 2


async def test_soft_delete_sets_moderated_flag(env):
    """soft_delete with moderated_by sets the moderated flag on the post."""
    post = _post("sp-mod-1")
    await env.repo.save(env.space_id, post)
    await env.repo.soft_delete("sp-mod-1", moderated_by="uid-admin")
    result = await env.repo.get("sp-mod-1")
    _, fetched = result
    assert fetched.deleted is True
    assert fetched.moderated is True


async def test_edit_post(env):
    """edit updates the post's content and sets edited_at."""
    post = _post("sp-edit-1")
    await env.repo.save(env.space_id, post)
    await env.repo.edit("sp-edit-1", "Updated content")
    _, fetched = await env.repo.get("sp-edit-1")
    assert fetched.content == "Updated content"
    assert fetched.edited_at is not None


# ── Reactions ─────────────────────────────────────────────────────────────


async def test_add_reaction(env):
    """add_reaction adds a user's reaction and returns updated post."""
    post = _post("sp-react-1")
    await env.repo.save(env.space_id, post)
    updated = await env.repo.add_reaction("sp-react-1", "👍", "uid-alice")
    assert "👍" in updated.reactions
    assert "uid-alice" in updated.reactions["👍"]


async def test_remove_reaction(env):
    """remove_reaction removes a user's reaction from the post."""
    post = _post("sp-react-2")
    await env.repo.save(env.space_id, post)
    await env.repo.add_reaction("sp-react-2", "❤️", "uid-alice")
    updated = await env.repo.remove_reaction("sp-react-2", "❤️", "uid-alice")
    assert "❤️" not in updated.reactions


async def test_add_reaction_concurrent(env):
    """Concurrent reaction adds are serialised correctly."""
    post = _post("sp-conc-1")
    await env.repo.save(env.space_id, post)
    # Run two additions concurrently
    await asyncio.gather(
        env.repo.add_reaction("sp-conc-1", "🎉", "uid-alice"),
        env.repo.add_reaction("sp-conc-1", "🎉", "uid-bob"),
    )
    _, fetched = await env.repo.get("sp-conc-1")
    assert len(fetched.reactions.get("🎉", frozenset())) == 2


# ── Comments ──────────────────────────────────────────────────────────────


async def test_add_and_list_comments(env):
    """add_comment persists a comment; list_comments retrieves it."""
    post = _post("sp-comm-1")
    await env.repo.save(env.space_id, post)
    comment = _comment("cmt-1", "sp-comm-1")
    await env.repo.add_comment(comment)
    comments = await env.repo.list_comments("sp-comm-1")
    assert len(comments) == 1
    assert comments[0].content == "Great post!"


async def test_get_comment(env):
    """get_comment retrieves a specific comment by id."""
    post = _post("sp-gc-1")
    await env.repo.save(env.space_id, post)
    comment = _comment("gcmt-1", "sp-gc-1")
    await env.repo.add_comment(comment)
    fetched = await env.repo.get_comment("gcmt-1")
    assert fetched is not None
    assert fetched.id == "gcmt-1"


async def test_soft_delete_comment(env):
    """soft_delete_comment marks the comment deleted."""
    post = _post("sp-dcom-1")
    await env.repo.save(env.space_id, post)
    comment = _comment("dcmt-1", "sp-dcom-1")
    await env.repo.add_comment(comment)
    await env.repo.soft_delete_comment("dcmt-1")
    fetched = await env.repo.get_comment("dcmt-1")
    assert fetched.deleted is True


async def test_list_comments_since_joins_through_post(env):
    """``list_comments_since`` filters by parent post's space_id (JOIN)."""
    # Post in our space.
    in_space = Post(
        id="sp-cs-1",
        author="uid-alice",
        type=PostType.TEXT,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        content="x",
    )
    await env.repo.save(env.space_id, in_space)
    # Comment on the in-space post (created today).
    c = Comment(
        id="cmt-now",
        post_id="sp-cs-1",
        author="uid-alice",
        type=CommentType.TEXT,
        created_at=datetime.now(timezone.utc),
        content="recent",
    )
    await env.repo.add_comment(c)
    # An older comment on the same post.
    old = Comment(
        id="cmt-old",
        post_id="sp-cs-1",
        author="uid-alice",
        type=CommentType.TEXT,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        content="old",
    )
    await env.repo.add_comment(old)

    cutoff = datetime(2025, 6, 1, tzinfo=timezone.utc).isoformat()
    rows = await env.repo.list_comments_since(env.space_id, cutoff)
    ids = [c.id for _, c in rows]
    # Only the recent comment's date passes ``cutoff``.
    assert "cmt-now" in ids
    assert "cmt-old" not in ids
    # Tuple shape: (post_id, Comment).
    post_ids = {pid for pid, _ in rows}
    assert post_ids == {"sp-cs-1"}


async def test_list_comments_since_excludes_soft_deleted(env):
    """Soft-deleted comments are not replayed on resume."""
    post = _post("sp-cs-2")
    await env.repo.save(env.space_id, post)
    c = Comment(
        id="cmt-del",
        post_id="sp-cs-2",
        author="uid-alice",
        type=CommentType.TEXT,
        created_at=datetime.now(timezone.utc),
        content="bye",
    )
    await env.repo.add_comment(c)
    await env.repo.soft_delete_comment("cmt-del")
    rows = await env.repo.list_comments_since(
        env.space_id,
        "2020-01-01T00:00:00+00:00",
    )
    assert "cmt-del" not in [c.id for _, c in rows]

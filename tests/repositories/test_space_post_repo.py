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


async def test_list_feed_excludes_deleted(env):
    """list_feed does not return soft-deleted posts."""
    post = _post("sp-del-1")
    await env.repo.save(env.space_id, post)
    await env.repo.soft_delete("sp-del-1", space_id=env.space_id)
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
    await env.repo.soft_delete("sp-deleted", space_id=env.space_id)
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
    await env.repo.soft_delete(
        "sp-mod-1", space_id=env.space_id, moderated_by="uid-admin"
    )
    result = await env.repo.get("sp-mod-1")
    _, fetched = result
    assert fetched.deleted is True
    assert fetched.moderated is True


async def test_edit_post(env):
    """edit updates the post's content and sets edited_at."""
    post = _post("sp-edit-1")
    await env.repo.save(env.space_id, post)
    await env.repo.edit("sp-edit-1", "Updated content", space_id=env.space_id)
    _, fetched = await env.repo.get("sp-edit-1")
    assert fetched.content == "Updated content"
    assert fetched.edited_at is not None


# ── Reactions ─────────────────────────────────────────────────────────────


async def test_add_reaction(env):
    """add_reaction adds a user's reaction and returns updated post."""
    post = _post("sp-react-1")
    await env.repo.save(env.space_id, post)
    updated = await env.repo.add_reaction(
        "sp-react-1", "👍", "uid-alice", space_id=env.space_id
    )
    assert "👍" in updated.reactions
    assert "uid-alice" in updated.reactions["👍"]


async def test_remove_reaction(env):
    """remove_reaction removes a user's reaction from the post."""
    post = _post("sp-react-2")
    await env.repo.save(env.space_id, post)
    await env.repo.add_reaction("sp-react-2", "❤️", "uid-alice", space_id=env.space_id)
    updated = await env.repo.remove_reaction(
        "sp-react-2", "❤️", "uid-alice", space_id=env.space_id
    )
    assert "❤️" not in updated.reactions


async def test_add_reaction_concurrent(env):
    """Concurrent reaction adds are serialised correctly."""
    post = _post("sp-conc-1")
    await env.repo.save(env.space_id, post)
    # Run two additions concurrently
    await asyncio.gather(
        env.repo.add_reaction("sp-conc-1", "🎉", "uid-alice", space_id=env.space_id),
        env.repo.add_reaction("sp-conc-1", "🎉", "uid-bob", space_id=env.space_id),
    )
    _, fetched = await env.repo.get("sp-conc-1")
    assert len(fetched.reactions.get("🎉", frozenset())) == 2


# ── Comments ──────────────────────────────────────────────────────────────


async def test_add_and_list_comments(env):
    """add_comment persists a comment; list_comments retrieves it."""
    post = _post("sp-comm-1")
    await env.repo.save(env.space_id, post)
    comment = _comment("cmt-1", "sp-comm-1")
    await env.repo.add_comment(comment, space_id=env.space_id)
    comments = await env.repo.list_comments("sp-comm-1")
    assert len(comments) == 1
    assert comments[0].content == "Great post!"


async def test_get_comment(env):
    """get_comment retrieves a specific comment by id."""
    post = _post("sp-gc-1")
    await env.repo.save(env.space_id, post)
    comment = _comment("gcmt-1", "sp-gc-1")
    await env.repo.add_comment(comment, space_id=env.space_id)
    fetched = await env.repo.get_comment("gcmt-1")
    assert fetched is not None
    assert fetched.id == "gcmt-1"


async def test_soft_delete_comment(env):
    """soft_delete_comment marks the comment deleted."""
    post = _post("sp-dcom-1")
    await env.repo.save(env.space_id, post)
    comment = _comment("dcmt-1", "sp-dcom-1")
    await env.repo.add_comment(comment, space_id=env.space_id)
    await env.repo.soft_delete_comment("dcmt-1", space_id=env.space_id)
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
    await env.repo.add_comment(c, space_id=env.space_id)
    # An older comment on the same post.
    old = Comment(
        id="cmt-old",
        post_id="sp-cs-1",
        author="uid-alice",
        type=CommentType.TEXT,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        content="old",
    )
    await env.repo.add_comment(old, space_id=env.space_id)

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
    await env.repo.add_comment(c, space_id=env.space_id)
    await env.repo.soft_delete_comment("cmt-del", space_id=env.space_id)
    rows = await env.repo.list_comments_since(
        env.space_id,
        "2020-01-01T00:00:00+00:00",
    )
    assert "cmt-del" not in [c.id for _, c in rows]


# ── Cross-space scoping (#693) ────────────────────────────────────────────
#
# The §24.11 pipeline gates an inbound envelope against ONE space id. Every
# mutator therefore takes that gated ``space_id`` and scopes its statement on
# it, so a household with a seat in space A can never name a row of space B.


@pytest.fixture
async def two(env):
    """``env`` plus a second space B, each holding one post + one comment."""
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        ("sp-B", "OtherSpace", "inst-y", "alice", "ccdd" * 16),
    )
    env.space_a = env.space_id
    env.space_b = "sp-B"
    await env.repo.save(env.space_a, _post("post-a"))
    await env.repo.save(env.space_b, _post("post-b"))
    await env.repo.add_comment(_comment("cmt-a", "post-a"), space_id=env.space_a)
    await env.repo.add_comment(_comment("cmt-b", "post-b"), space_id=env.space_b)
    await env.repo.add_reaction("post-b", "👍", "uid-bob", space_id=env.space_b)
    yield env


async def _post_row(env, post_id: str) -> dict:
    row = await env.db.fetchone("SELECT * FROM space_posts WHERE id=?", (post_id,))
    return {k: row[k] for k in row.keys()}


async def _comment_row(env, comment_id: str) -> dict:
    row = await env.db.fetchone(
        "SELECT * FROM space_post_comments WHERE id=?",
        (comment_id,),
    )
    return {k: row[k] for k in row.keys()}


async def test_save_refuses_upsert_onto_another_spaces_post(two):
    """An id owned by B cannot be re-saved under A — B's row is untouched."""
    before = await _post_row(two, "post-b")
    from dataclasses import replace

    evil = replace(_post("post-b"), content="pwned")
    assert await two.repo.save(two.space_a, evil) is None
    assert await _post_row(two, "post-b") == before
    # And no shadow row was created in A.
    rows = await two.db.fetchall(
        "SELECT id FROM space_posts WHERE space_id=?",
        (two.space_a,),
    )
    assert [r["id"] for r in rows] == ["post-a"]


async def test_save_same_space_upsert_still_works(two):
    """The own-space upsert path is unchanged and returns the post."""
    from dataclasses import replace

    updated = replace(_post("post-b"), content="legit edit")
    saved = await two.repo.save(two.space_b, updated)
    assert saved is not None
    got = await two.repo.get("post-b")
    assert got[1].content == "legit edit"


async def test_soft_delete_is_space_scoped(two):
    before = await _post_row(two, "post-b")
    assert await two.repo.soft_delete("post-b", space_id=two.space_a) is False
    assert await _post_row(two, "post-b") == before
    assert await two.repo.soft_delete("post-b", space_id=two.space_b) is True
    assert (await two.repo.get("post-b"))[1].deleted is True


async def test_edit_is_space_scoped(two):
    before = await _post_row(two, "post-b")
    assert await two.repo.edit("post-b", "pwned", space_id=two.space_a) is False
    assert await _post_row(two, "post-b") == before
    assert await two.repo.edit("post-b", "fine", space_id=two.space_b) is True
    assert (await two.repo.get("post-b"))[1].content == "fine"


async def test_add_reaction_is_space_scoped(two):
    before = await _post_row(two, "post-b")
    with pytest.raises(KeyError):
        await two.repo.add_reaction("post-b", "🎉", "uid-eve", space_id=two.space_a)
    assert await _post_row(two, "post-b") == before
    updated = await two.repo.add_reaction(
        "post-b", "🎉", "uid-alice", space_id=two.space_b
    )
    assert "uid-alice" in updated.reactions["🎉"]


async def test_remove_reaction_is_space_scoped(two):
    before = await _post_row(two, "post-b")
    with pytest.raises(KeyError):
        await two.repo.remove_reaction("post-b", "👍", "uid-bob", space_id=two.space_a)
    assert await _post_row(two, "post-b") == before
    updated = await two.repo.remove_reaction(
        "post-b", "👍", "uid-bob", space_id=two.space_b
    )
    assert "👍" not in updated.reactions


async def test_increment_comment_count_is_space_scoped(two):
    before = await _post_row(two, "post-b")
    assert (
        await two.repo.increment_comment_count("post-b", space_id=two.space_a) is False
    )
    assert await _post_row(two, "post-b") == before
    assert (
        await two.repo.increment_comment_count("post-b", space_id=two.space_b) is True
    )
    assert (await _post_row(two, "post-b"))["comment_count"] == before[
        "comment_count"
    ] + 1


async def test_decrement_comment_count_is_space_scoped(two):
    await two.repo.increment_comment_count("post-b", space_id=two.space_b)
    before = await _post_row(two, "post-b")
    assert (
        await two.repo.decrement_comment_count("post-b", space_id=two.space_a) is False
    )
    assert await _post_row(two, "post-b") == before
    assert (
        await two.repo.decrement_comment_count("post-b", space_id=two.space_b) is True
    )
    assert (await _post_row(two, "post-b"))["comment_count"] == before[
        "comment_count"
    ] - 1


async def test_add_comment_is_space_scoped(two):
    """A comment on B's post cannot be inserted under A's gated space."""
    assert (
        await two.repo.add_comment(_comment("cmt-evil", "post-b"), space_id=two.space_a)
        is False
    )
    assert await two.repo.get_comment("cmt-evil") is None
    assert (
        await two.repo.add_comment(_comment("cmt-ok", "post-b"), space_id=two.space_b)
        is True
    )
    assert await two.repo.get_comment("cmt-ok") is not None


async def test_soft_delete_comment_is_space_scoped(two):
    before = await _comment_row(two, "cmt-b")
    assert await two.repo.soft_delete_comment("cmt-b", space_id=two.space_a) is False
    assert await _comment_row(two, "cmt-b") == before
    assert await two.repo.soft_delete_comment("cmt-b", space_id=two.space_b) is True
    assert (await two.repo.get_comment("cmt-b")).deleted is True


async def test_edit_comment_is_space_scoped(two):
    before = await _comment_row(two, "cmt-b")
    assert await two.repo.edit_comment("cmt-b", "pwned", space_id=two.space_a) is False
    assert await _comment_row(two, "cmt-b") == before
    assert await two.repo.edit_comment("cmt-b", "fine", space_id=two.space_b) is True
    assert (await two.repo.get_comment("cmt-b")).content == "fine"

"""Tests for SqliteSpacePostRepo — space feed posts, reactions, comments."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from social_home.domain.post import Comment, CommentType, Post, PostType
from social_home.repositories.space_post_repo import SqliteSpacePostRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with a space post repo and a seeded space + user."""
    from social_home.crypto import generate_identity_keypair, derive_instance_id
    from social_home.db.database import AsyncDatabase

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


async def test_list_feed_excludes_deleted(env):
    """list_feed does not return soft-deleted posts."""
    post = _post("sp-del-1")
    await env.repo.save(env.space_id, post)
    await env.repo.soft_delete("sp-del-1")
    results = await env.repo.list_feed(env.space_id)
    assert not any(p.id == "sp-del-1" for p in results)


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

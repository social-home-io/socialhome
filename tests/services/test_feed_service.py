"""Tests for social_home.services.feed_service."""

from __future__ import annotations


import pytest

from social_home.crypto import generate_identity_keypair, derive_instance_id
from social_home.db.database import AsyncDatabase
from social_home.domain.post import FileMeta, PostType
from social_home.infrastructure.event_bus import EventBus
from social_home.repositories.post_repo import SqlitePostRepo
from social_home.repositories.user_repo import SqliteUserRepo
from social_home.services.feed_service import FeedService
from social_home.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    """Full service stack for feed service tests."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    post_repo = SqlitePostRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    feed_svc = FeedService(post_repo, user_repo, bus)

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_svc = user_svc
    s.feed_svc = feed_svc

    async def provision_user(username, **kw):
        return await user_svc.provision(username=username, display_name=username, **kw)

    s.provision_user = provision_user
    yield s
    await db.shutdown()


async def test_create_and_list(stack):
    """Created post appears in the feed."""
    u = await stack.provision_user("pascal")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="hello",
    )
    assert p.content == "hello"
    feed = await stack.feed_svc.list_feed(limit=10)
    assert len(feed) == 1


async def test_edit_and_delete(stack):
    """Editing a post updates content; deleting it raises KeyError on retrieval."""
    u = await stack.provision_user("pascal")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="v1",
    )
    updated = await stack.feed_svc.edit_post(
        p.id,
        editor_user_id=u.user_id,
        new_content="v2",
    )
    assert updated.content == "v2"
    await stack.feed_svc.delete_post(p.id, actor_user_id=u.user_id)
    with pytest.raises(KeyError):
        await stack.feed_svc.get_post(p.id)


async def test_non_author_cannot_edit(stack):
    """Non-author editing raises PermissionError."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    p = await stack.feed_svc.create_post(
        author_user_id=a.user_id,
        type=PostType.TEXT,
        content="x",
    )
    with pytest.raises(PermissionError):
        await stack.feed_svc.edit_post(p.id, editor_user_id=b.user_id, new_content="y")


async def test_admin_can_edit_others(stack):
    """An admin user can edit another user's post."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob", is_admin=True)
    p = await stack.feed_svc.create_post(
        author_user_id=a.user_id,
        type=PostType.TEXT,
        content="x",
    )
    updated = await stack.feed_svc.edit_post(
        p.id, editor_user_id=b.user_id, new_content="admin"
    )
    assert updated.content == "admin"


async def test_reactions(stack):
    """add_reaction and remove_reaction update the reactions mapping."""
    u = await stack.provision_user("pascal")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="x",
    )
    r = await stack.feed_svc.add_reaction(p.id, user_id=u.user_id, emoji="👍")
    assert r.reactions["👍"] == frozenset({u.user_id})
    r2 = await stack.feed_svc.remove_reaction(p.id, user_id=u.user_id, emoji="👍")
    assert "👍" not in r2.reactions


async def test_comments(stack):
    """Adding a comment increments comment_count; deleting decrements it."""
    u = await stack.provision_user("pascal")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="x",
    )
    c = await stack.feed_svc.add_comment(
        p.id,
        author_user_id=u.user_id,
        content="nice",
    )
    assert (await stack.feed_svc.get_post(p.id)).comment_count == 1
    await stack.feed_svc.delete_comment(c.id, actor_user_id=u.user_id)
    assert (await stack.feed_svc.get_post(p.id)).comment_count == 0


async def test_bookmarks(stack):
    """bookmark adds a post to the user's bookmarks; unbookmark removes it."""
    u = await stack.provision_user("pascal")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="x",
    )
    await stack.feed_svc.bookmark(u.user_id, p.id)
    bms = await stack.feed_svc.list_bookmarks(u.user_id)
    assert [b.id for b in bms] == [p.id]
    await stack.feed_svc.unbookmark(u.user_id, p.id)
    assert await stack.feed_svc.list_bookmarks(u.user_id) == []


async def test_text_post_requires_content(stack):
    """Creating a text post with blank content raises ValueError."""
    u = await stack.provision_user("pascal")
    with pytest.raises(ValueError):
        await stack.feed_svc.create_post(
            author_user_id=u.user_id,
            type=PostType.TEXT,
            content="   ",
        )


async def test_file_post_requires_meta(stack):
    """Creating a file post without file_meta raises ValueError."""
    u = await stack.provision_user("pascal")
    with pytest.raises(ValueError):
        await stack.feed_svc.create_post(
            author_user_id=u.user_id,
            type=PostType.FILE,
        )


async def test_list_feed_pagination(stack):
    """list_feed respects limit and before-cursor pagination."""
    u = await stack.provision_user("pascal")
    for i in range(5):
        await stack.feed_svc.create_post(
            author_user_id=u.user_id,
            type=PostType.TEXT,
            content=f"post {i}",
        )
    feed = await stack.feed_svc.list_feed(limit=3)
    assert len(feed) == 3
    page2 = await stack.feed_svc.list_feed(
        before=feed[-1].created_at.isoformat(),
        limit=3,
    )
    assert len(page2) == 2


async def test_image_post_accepted(stack):
    """Image post with media_url is accepted."""
    u = await stack.provision_user("a")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id,
        type="image",
        media_url="/media/x.webp",
        content="caption",
    )
    assert p.type is PostType.IMAGE


async def test_file_post_with_meta(stack):
    """File post with file_meta is accepted."""
    u = await stack.provision_user("a")
    fm = FileMeta(
        url="/x.pdf",
        mime_type="application/pdf",
        original_name="spec.pdf",
        size_bytes=1024,
    )
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.FILE,
        file_meta=fm,
    )
    assert p.file_meta.size_bytes == 1024


async def test_add_comment_image_requires_url(stack):
    """Image comment without media_url raises ValueError."""
    u = await stack.provision_user("a")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(ValueError, match="media_url"):
        await stack.feed_svc.add_comment(
            p.id, author_user_id=u.user_id, comment_type="image"
        )


async def test_bookmark_nonexistent_post(stack):
    """Bookmarking a nonexistent post raises KeyError."""
    u = await stack.provision_user("a")
    with pytest.raises(KeyError):
        await stack.feed_svc.bookmark(u.user_id, "nonexistent")


async def test_unknown_author(stack):
    """Creating a post with unknown author raises KeyError."""
    with pytest.raises(KeyError):
        await stack.feed_svc.create_post(
            author_user_id="ghost", type=PostType.TEXT, content="x"
        )


async def test_feed_comment_image_type(stack):
    """Image comment without media_url raises ValueError."""
    u = await stack.provision_user("imgtest")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id, type="text", content="x"
    )
    with pytest.raises(ValueError, match="media_url"):
        await stack.feed_svc.add_comment(
            p.id, author_user_id=u.user_id, comment_type="image"
        )


async def test_feed_comment_parent_wrong_post(stack):
    """Comment with parent_id from different post raises KeyError."""
    u = await stack.provision_user("parenttest")
    p1 = await stack.feed_svc.create_post(
        author_user_id=u.user_id, type="text", content="a"
    )
    p2 = await stack.feed_svc.create_post(
        author_user_id=u.user_id, type="text", content="b"
    )
    c = await stack.feed_svc.add_comment(p1.id, author_user_id=u.user_id, content="c")
    with pytest.raises(KeyError, match="parent"):
        await stack.feed_svc.add_comment(
            p2.id, author_user_id=u.user_id, content="r", parent_id=c.id
        )


async def test_feed_delete_comment_already_deleted(stack):
    """Deleting an already-deleted comment is a no-op."""
    u = await stack.provision_user("deltest")
    p = await stack.feed_svc.create_post(
        author_user_id=u.user_id, type="text", content="x"
    )
    c = await stack.feed_svc.add_comment(p.id, author_user_id=u.user_id, content="c")
    await stack.feed_svc.delete_comment(c.id, actor_user_id=u.user_id)
    await stack.feed_svc.delete_comment(c.id, actor_user_id=u.user_id)  # no-op


async def test_feed_over_length_content(stack):
    """Content exceeding MAX_POST_LENGTH raises ValueError."""
    u = await stack.provision_user("longtest")
    with pytest.raises(ValueError, match="maximum length"):
        await stack.feed_svc.create_post(
            author_user_id=u.user_id, type="text", content="x" * 10001
        )


async def test_feed_unknown_post_type(stack):
    """Invalid post type string raises ValueError."""
    u = await stack.provision_user("typetest")
    with pytest.raises(ValueError, match="invalid post type"):
        await stack.feed_svc.create_post(author_user_id=u.user_id, type="bogus")


async def test_feed_inactive_author_rejected(stack):
    """Inactive (deprovisioned) user cannot create posts."""
    u = await stack.provision_user("inactive")
    await stack.user_svc.deprovision("inactive")
    with pytest.raises(PermissionError, match="not active"):
        await stack.feed_svc.create_post(
            author_user_id=u.user_id, type="text", content="x"
        )

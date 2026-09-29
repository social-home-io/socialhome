"""Tests for socialhome.repositories.post_repo."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.post import LocationData, Post, PostType


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with post and user repos over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.post_repo import SqlitePostRepo
    from socialhome.repositories.user_repo import SqliteUserRepo
    from socialhome.services.feed_service import FeedService
    from socialhome.services.user_service import UserService

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    bus = EventBus()

    class Env:
        pass

    e = Env()
    e.db = db
    e.user_repo = SqliteUserRepo(db)
    e.post_repo = SqlitePostRepo(db)
    e.user_svc = UserService(e.user_repo, bus, own_instance_public_key=kp.public_key)
    e.feed_svc = FeedService(e.post_repo, e.user_repo, bus)
    yield e
    await db.shutdown()


async def test_image_urls_round_trip(env):
    """Multi-image posts persist their ``image_urls`` tuple in order."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    saved = await env.post_repo.save(
        Post(
            id="post-img-1",
            author=u.user_id,
            type=PostType.IMAGE,
            created_at=datetime.now(timezone.utc),
            image_urls=(
                "/api/media/a.webp",
                "/api/media/b.webp",
                "/api/media/c.webp",
            ),
        ),
    )
    got = await env.post_repo.get(saved.id)
    assert got is not None
    assert got.media_url is None
    assert got.image_urls == (
        "/api/media/a.webp",
        "/api/media/b.webp",
        "/api/media/c.webp",
    )


async def test_image_urls_empty_for_non_image_posts(env):
    """A text post round-trips with ``image_urls=()``."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    p = await env.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="just text",
    )
    got = await env.feed_svc.get_post(p.id)
    assert got.image_urls == ()


async def test_save_and_get_post(env):
    """A post created via feed_svc can be retrieved by ID."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    p = await env.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="hello",
    )
    got = await env.feed_svc.get_post(p.id)
    assert got.id == p.id
    assert got.content == "hello"


async def test_list_feed(env):
    """list_feed returns all created posts in reverse chronological order."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    for i in range(3):
        await env.feed_svc.create_post(
            author_user_id=u.user_id,
            type=PostType.TEXT,
            content=f"post {i}",
        )
    feed = await env.feed_svc.list_feed(limit=10)
    assert len(feed) == 3


async def test_get_missing_post_raises(env):
    """Getting a nonexistent post raises KeyError."""
    with pytest.raises(KeyError):
        await env.feed_svc.get_post("nonexistent")


async def test_location_post_round_trip(env):
    """A LOCATION post persists its lat/lon/label through location_json
    and rehydrates into a LocationData on read."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    saved = await env.post_repo.save(
        Post(
            id="post-loc-1",
            author=u.user_id,
            type=PostType.LOCATION,
            created_at=datetime.now(timezone.utc),
            content="Beach day",
            location=LocationData(lat=52.5200, lon=4.0600, label="Marina"),
        ),
    )
    got = await env.post_repo.get(saved.id)
    assert got is not None
    assert got.type is PostType.LOCATION
    assert got.location is not None
    assert got.location.lat == 52.5200
    assert got.location.lon == 4.0600
    assert got.location.label == "Marina"


async def test_location_post_label_optional(env):
    """label is None ⇒ stored omitted; round-trips back as None."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    await env.post_repo.save(
        Post(
            id="post-loc-2",
            author=u.user_id,
            type=PostType.LOCATION,
            created_at=datetime.now(timezone.utc),
            location=LocationData(lat=10.0, lon=20.0),
        ),
    )
    got = await env.post_repo.get("post-loc-2")
    assert got is not None and got.location is not None
    assert got.location.label is None


async def test_non_location_post_keeps_location_none(env):
    """A regular text post round-trips with location=None."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    p = await env.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="hi",
    )
    got = await env.feed_svc.get_post(p.id)
    assert got.location is None


# ── Read watermark ─────────────────────────────────────────────────────────


async def test_read_watermark_absent_by_default(env):
    await env.user_svc.provision(username="alice", display_name="Alice")
    row = await env.post_repo.get_read_watermark("uid-does-not-exist")
    assert row is None


async def test_set_and_get_read_watermark(env):
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    await env.post_repo.set_read_watermark(u.user_id, "post-1")
    got = await env.post_repo.get_read_watermark(u.user_id)
    assert got is not None
    assert got["last_read_post_id"] == "post-1"
    assert got["last_read_at"]


async def test_set_read_watermark_upserts(env):
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    await env.post_repo.set_read_watermark(u.user_id, "post-a")
    await env.post_repo.set_read_watermark(u.user_id, "post-b")
    got = await env.post_repo.get_read_watermark(u.user_id)
    assert got["last_read_post_id"] == "post-b"


async def test_set_read_watermark_accepts_none(env):
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    await env.post_repo.set_read_watermark(u.user_id, "post-a")
    await env.post_repo.set_read_watermark(u.user_id, None)
    got = await env.post_repo.get_read_watermark(u.user_id)
    assert got is not None
    assert got["last_read_post_id"] is None


async def test_latest_comment_per_post_empty_input(env):
    """Empty ``post_ids`` short-circuits to an empty dict — never sends
    a malformed ``IN ()`` clause to SQLite."""
    out = await env.post_repo.latest_comment_per_post([])
    assert out == {}


async def test_latest_comment_per_post_picks_newest_skips_deleted(env):
    """The window-function query returns one row per post (the newest
    non-deleted comment).  Posts with zero or only-deleted comments
    are omitted from the result dict."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    p_silent = await env.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="silent",
    )
    p_loud = await env.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="loud",
    )
    await env.feed_svc.add_comment(
        p_loud.id,
        author_user_id=u.user_id,
        content="first",
    )
    second = await env.feed_svc.add_comment(
        p_loud.id,
        author_user_id=u.user_id,
        content="second",
    )
    third = await env.feed_svc.add_comment(
        p_loud.id,
        author_user_id=u.user_id,
        content="third",
    )

    out = await env.post_repo.latest_comment_per_post(
        [p_silent.id, p_loud.id],
    )
    assert p_silent.id not in out
    assert out[p_loud.id].id == third.id

    # Soft-delete the latest — preview should fall back to the prior
    # comment, not to the (also deleted) row.
    await env.feed_svc.delete_comment(third.id, actor_user_id=u.user_id)
    out = await env.post_repo.latest_comment_per_post([p_loud.id])
    assert out[p_loud.id].id == second.id

    # Soft-delete every remaining comment → the post drops out of the
    # result dict entirely.
    await env.feed_svc.delete_comment(second.id, actor_user_id=u.user_id)
    first_id = (await env.post_repo.latest_comment_per_post([p_loud.id]))[p_loud.id].id
    await env.feed_svc.delete_comment(first_id, actor_user_id=u.user_id)
    out = await env.post_repo.latest_comment_per_post([p_loud.id])
    assert out == {}


async def test_link_preview_round_trip_and_cleared_on_delete(env):
    from socialhome.domain.link_preview import LinkPreview

    u = await env.user_svc.provision(username="lp", display_name="LP")
    preview = LinkPreview(
        url="https://example.com/", title="T", thumbnail_url="api/media/x.webp"
    )
    await env.post_repo.save(
        Post(
            id="post-lp-1",
            author=u.user_id,
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content="see https://example.com/",
            link_preview=preview,
        )
    )
    fetched = await env.post_repo.get("post-lp-1")
    assert fetched is not None and fetched.link_preview == preview
    await env.post_repo.soft_delete("post-lp-1")
    gone = await env.post_repo.get("post-lp-1")
    assert gone is not None and gone.link_preview is None

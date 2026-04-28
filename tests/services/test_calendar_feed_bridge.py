"""Tests for :class:`CalendarFeedBridge` — Phase B feed surfacing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
)
from socialhome.domain.post import PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.calendar_repo import SqliteSpaceCalendarRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.services.calendar_feed_bridge import CalendarFeedBridge
from socialhome.services.calendar_service import SpaceCalendarService


@pytest.fixture
async def env(tmp_dir):
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
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("sp-feed", "FeedSpace", iid, "alice", kp.public_key.hex()),
    )
    bus = EventBus()
    space_cal_repo = SqliteSpaceCalendarRepo(db)
    space_post_repo = SqliteSpacePostRepo(db)
    space_cal_svc = SpaceCalendarService(space_cal_repo, bus)
    bridge = CalendarFeedBridge(
        bus=bus,
        post_repo=space_post_repo,
        calendar_repo=space_cal_repo,
    )
    bridge.wire()

    class E:
        pass

    e = E()
    e.db = db
    e.bus = bus
    e.cal_svc = space_cal_svc
    e.cal_repo = space_cal_repo
    e.post_repo = space_post_repo
    yield e
    await db.shutdown()


async def test_create_event_creates_feed_post(env):
    """A new calendar event spawns a PostType.EVENT post with linked_event_id."""
    now = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
    event = await env.cal_svc.create_event(
        space_id="sp-feed",
        summary="Summer party",
        start=now.isoformat(),
        end=(now + timedelta(hours=4)).isoformat(),
        created_by="uid-alice",
    )
    # Bridge fires synchronously on the bus.
    feed = await env.post_repo.list_feed("sp-feed")
    assert len(feed) == 1
    assert feed[0].type is PostType.EVENT
    assert feed[0].content == "Summer party"
    assert feed[0].linked_event_id == event.id
    assert feed[0].author == "uid-alice"


async def test_event_update_rewrites_post_body(env):
    """Renaming the event updates the linked post's content."""
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    event = await env.cal_svc.create_event(
        space_id="sp-feed",
        summary="Old title",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.cal_svc.update_event(event.id, summary="New title")
    feed = await env.post_repo.list_feed("sp-feed")
    assert len(feed) == 1
    assert feed[0].content == "New title"
    assert feed[0].linked_event_id == event.id


async def test_event_update_no_body_change_is_noop(env):
    """If the title didn't change, the post isn't touched."""
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    event = await env.cal_svc.create_event(
        space_id="sp-feed",
        summary="Same title",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    pre = (await env.post_repo.list_feed("sp-feed"))[0]
    await env.cal_svc.update_event(event.id, summary="Same title")
    post = (await env.post_repo.list_feed("sp-feed"))[0]
    assert post.edited_at == pre.edited_at  # no edit happened


async def test_event_delete_soft_deletes_post(env):
    """Deleting the event soft-deletes the linked post."""
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    event = await env.cal_svc.create_event(
        space_id="sp-feed",
        summary="Cancelled event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.cal_svc.delete_event(event.id)
    # list_feed filters out deleted posts; the row still exists with deleted=1.
    got = await env.post_repo.get_by_linked_event_id(event.id)
    assert got is not None
    _, post = got
    assert post.deleted is True


async def test_duplicate_create_is_idempotent(env):
    """Receiving the same CalendarEventCreated twice doesn't double-post."""
    from socialhome.domain.calendar import CalendarEvent

    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ev = CalendarEvent(
        id="ev-twice",
        calendar_id="sp-feed",
        summary="Replay party",
        start=now,
        end=now,
        created_by="uid-alice",
    )
    await env.cal_repo.save_event("sp-feed", ev)
    # Fire two CalendarEventCreated bus events with the same event id —
    # simulates federation replay landing at the inbound handler twice.
    await env.bus.publish(CalendarEventCreated(event=ev))
    await env.bus.publish(CalendarEventCreated(event=ev))
    feed = await env.post_repo.list_feed("sp-feed")
    assert len(feed) == 1


async def test_get_by_linked_event_id_returns_none_for_no_match(env):
    assert await env.post_repo.get_by_linked_event_id("does-not-exist") is None


async def test_recurring_event_creates_one_post(env):
    """A weekly recurring event yields one feed post, not one per occurrence."""
    seed = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    await env.cal_svc.create_event(
        space_id="sp-feed",
        summary="Weekly meet",
        start=seed.isoformat(),
        end=(seed + timedelta(minutes=30)).isoformat(),
        created_by="uid-alice",
        rrule="FREQ=WEEKLY;COUNT=10",
    )
    feed = await env.post_repo.list_feed("sp-feed")
    assert len(feed) == 1
    assert feed[0].type is PostType.EVENT


async def test_event_delete_event_emits_event_unused(env):
    """CalendarEventDeleted for an event without a linked post is a no-op."""
    await env.bus.publish(CalendarEventDeleted(event_id="never-existed"))
    # No exception, no rows.
    feed = await env.post_repo.list_feed("sp-feed")
    assert feed == []

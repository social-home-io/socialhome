"""Tests for :class:`CalendarFeedBridge` — Phase B feed surfacing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.repositories.space_repo import SqliteSpaceRepo

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
)
from socialhome.domain.post import PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.calendar_repo import SqliteSpaceCalendarRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
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
    space_cal_svc.attach_space_repo(SqliteSpaceRepo(db))
    remote_members = SqliteSpaceRemoteMemberRepo(db)
    bridge = CalendarFeedBridge(
        bus=bus,
        post_repo=space_post_repo,
        calendar_repo=space_cal_repo,
        space_repo=SqliteSpaceRepo(db),
        remote_member_repo=remote_members,
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
    e.iid = iid
    e.remote_members = remote_members
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
        announce_in_feed=True,
    )
    # Bridge fires synchronously on the bus.
    feed = await env.post_repo.list_feed("sp-feed")
    assert len(feed) == 1
    assert feed[0].type is PostType.EVENT
    assert feed[0].content == "Summer party"
    assert feed[0].linked_event_id == event.id
    assert feed[0].author == "uid-alice"


async def test_event_without_announce_makes_no_feed_post(env):
    """§23.15 — the feed mirror is opt-in. An event created without
    announce_in_feed lives only in the Calendar tab; the bridge skips it."""
    now = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
    await env.cal_svc.create_event(
        space_id="sp-feed",
        summary="Quiet event",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-alice",
        # announce_in_feed defaults False
    )
    feed = await env.post_repo.list_feed("sp-feed")
    assert feed == []


async def test_event_update_rewrites_post_body(env):
    """Renaming the event updates the linked post's content."""
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    event = await env.cal_svc.create_event(
        space_id="sp-feed",
        summary="Old title",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        announce_in_feed=True,
    )
    await env.cal_svc.update_event(
        event.id, actor_user_id="u-test", space_id="sp-feed", summary="New title"
    )
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
        announce_in_feed=True,
    )
    pre = (await env.post_repo.list_feed("sp-feed"))[0]
    await env.cal_svc.update_event(
        event.id, actor_user_id="u-test", space_id="sp-feed", summary="Same title"
    )
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
        announce_in_feed=True,
    )
    await env.cal_svc.delete_event(event.id, actor_user_id="u-test", space_id="sp-feed")
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
        announce_in_feed=True,
    )
    await env.cal_repo.save_event(ev, space_id="sp-feed")
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
        announce_in_feed=True,
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


# ─── The mirror is a post: the space's ``posts`` level gates it (§4.3) ───


async def _announced(env, *, created_by: str, event_id: str) -> list:
    """An announced event lands the way an inbound one does — straight in
    the repo, then ``CalendarEventCreated`` — and the feed is read back."""
    from socialhome.domain.calendar import CalendarEvent

    now = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
    ev = CalendarEvent(
        id=event_id,
        calendar_id="sp-feed",
        summary="Announced",
        start=now,
        end=now + timedelta(hours=1),
        created_by=created_by,
        announce_in_feed=True,
    )
    assert await env.cal_repo.save_event(ev, space_id="sp-feed")
    await env.bus.publish(CalendarEventCreated(event=ev))
    return [
        p
        for p in await env.post_repo.list_feed("sp-feed")
        if p.linked_event_id == event_id
    ]


async def _seat_local(env, user_id: str, role: str) -> None:
    await env.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-feed', ?, ?)",
        (user_id, role),
    )


async def _posts_level(env, level: str) -> None:
    await env.db.enqueue(
        "UPDATE spaces SET posts_access=? WHERE id='sp-feed'", (level,)
    )


async def test_admin_only_posts_skip_a_members_announcement(env):
    await _seat_local(env, "uid-mem", "member")
    await _seat_local(env, "uid-mod", "moderator")
    await _posts_level(env, "admin_only")
    assert await _announced(env, created_by="uid-mem", event_id="e-mem") == []
    assert await _announced(env, created_by="uid-mod", event_id="e-mod") == []


async def test_admin_only_posts_keep_an_admins_announcement(env):
    await _seat_local(env, "uid-adm", "admin")
    await _seat_local(env, "uid-alice", "owner")
    await _posts_level(env, "admin_only")
    assert len(await _announced(env, created_by="uid-adm", event_id="e-adm")) == 1
    assert len(await _announced(env, created_by="uid-alice", event_id="e-own")) == 1


async def test_moderated_posts_skip_a_members_announcement(env):
    """A mirrored post bypasses the review queue, so under MODERATED only
    content authority announces."""
    await _seat_local(env, "uid-mem", "member")
    await _seat_local(env, "uid-mod", "moderator")
    await _posts_level(env, "moderated")
    assert await _announced(env, created_by="uid-mem", event_id="e-mem") == []
    assert len(await _announced(env, created_by="uid-mod", event_id="e-mod")) == 1


async def test_a_remote_creators_seat_decides(env):
    """Remote creators are judged by their mirrored seat."""
    await _posts_level(env, "admin_only")
    for inst, uid, role in (
        ("inst-b", "uid-r-adm", "admin"),
        ("inst-b", "uid-r-mem", "member"),
        ("inst-c", "uid-r-mod", "moderator"),
    ):
        await env.remote_members.add(
            space_id="sp-feed",
            instance_id=inst,
            user_id=uid,
            user_pk=None,
            display_name=uid,
            role=role,
        )
    assert len(await _announced(env, created_by="uid-r-adm", event_id="e-1")) == 1
    assert await _announced(env, created_by="uid-r-mem", event_id="e-2") == []
    assert await _announced(env, created_by="uid-r-mod", event_id="e-3") == []
    assert await _announced(env, created_by="uid-nobody", event_id="e-4") == []


async def test_the_hosts_people_are_seated_as_members_and_still_announce(env):
    """A member household's mirror seats the space's owner as a plain
    ``member`` of the host; the host refuses its plain members' announcements
    at the source, so a host-seated writer passes (``SpaceAuthorship`` rule)."""
    await env.db.enqueue(
        "UPDATE spaces SET owner_instance_id='inst-host' WHERE id='sp-feed'"
    )
    await _posts_level(env, "admin_only")
    await env.remote_members.add(
        space_id="sp-feed",
        instance_id="inst-host",
        user_id="uid-owner",
        user_pk=None,
        display_name="Owner",
    )
    assert len(await _announced(env, created_by="uid-owner", event_id="e-o")) == 1


async def test_open_posts_announce_for_anyone(env):
    assert len(await _announced(env, created_by="uid-whoever", event_id="e-x")) == 1

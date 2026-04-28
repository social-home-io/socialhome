"""Tests for :class:`SpaceCalendarReminderScheduler` (Phase D)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import EventReminderDue
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.calendar_repo import SqliteSpaceCalendarRepo
from socialhome.services.calendar_service import SpaceCalendarService
from socialhome.services.space_calendar_reminder_scheduler import (
    SpaceCalendarReminderScheduler,
)


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
        ("sp-rem", "Reminder space", iid, "alice", kp.public_key.hex()),
    )
    bus = EventBus()
    repo = SqliteSpaceCalendarRepo(db)
    svc = SpaceCalendarService(repo, bus)
    sched = SpaceCalendarReminderScheduler(
        calendar_repo=repo, bus=bus,
    )

    class E:
        pass

    e = E()
    e.db = db
    e.bus = bus
    e.repo = repo
    e.svc = svc
    e.sched = sched
    yield e
    await db.shutdown()


async def test_add_reminder_persists(env):
    now = datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc)
    event = await env.svc.create_event(
        space_id="sp-rem",
        summary="Concert",
        start=now.isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
        created_by="uid-alice",
    )
    reminder = await env.svc.add_reminder(
        event_id=event.id,
        user_id="uid-alice",
        minutes_before=60,
    )
    assert reminder.minutes_before == 60
    rems = await env.svc.list_reminders(
        event_id=event.id, user_id="uid-alice",
    )
    assert len(rems) == 1


async def test_remove_reminder_clears(env):
    now = datetime(2030, 6, 1, tzinfo=timezone.utc)
    event = await env.svc.create_event(
        space_id="sp-rem",
        summary="Concert",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.svc.add_reminder(
        event_id=event.id, user_id="uid-alice", minutes_before=60,
    )
    await env.svc.remove_reminder(
        event_id=event.id, user_id="uid-alice", minutes_before=60,
    )
    rems = await env.svc.list_reminders(
        event_id=event.id, user_id="uid-alice",
    )
    assert rems == []


async def test_negative_minutes_rejected(env):
    now = datetime(2030, 7, 1, tzinfo=timezone.utc)
    event = await env.svc.create_event(
        space_id="sp-rem",
        summary="Trip",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    with pytest.raises(ValueError, match="minutes_before"):
        await env.svc.add_reminder(
            event_id=event.id, user_id="uid-alice", minutes_before=-1,
        )


async def test_scheduler_fires_due_reminder(env):
    """A reminder whose fire_at is in the past triggers EventReminderDue."""
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    event = await env.svc.create_event(
        space_id="sp-rem",
        summary="Already started",
        start=past.isoformat(),
        end=(past + timedelta(minutes=30)).isoformat(),
        created_by="uid-alice",
    )
    # Add a reminder for "10 min before" — fire_at = past-10 min, which
    # is well in the past so it's due immediately.
    await env.svc.add_reminder(
        event_id=event.id, user_id="uid-alice", minutes_before=10,
    )
    received: list[EventReminderDue] = []

    async def _capture(evt):
        received.append(evt)

    env.bus.subscribe(EventReminderDue, _capture)
    fired = await env.sched.tick_once()
    assert fired == 1
    assert len(received) == 1
    assert received[0].user_id == "uid-alice"
    assert received[0].summary == "Already started"


async def test_scheduler_skips_already_sent(env):
    """A reminder marked sent doesn't fire again."""
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    event = await env.svc.create_event(
        space_id="sp-rem",
        summary="Started",
        start=past.isoformat(),
        end=past.isoformat(),
        created_by="uid-alice",
    )
    await env.svc.add_reminder(
        event_id=event.id, user_id="uid-alice", minutes_before=10,
    )
    await env.sched.tick_once()
    received: list = []
    env.bus.subscribe(EventReminderDue, lambda e: received.append(e))
    # Second tick — nothing due.
    fired = await env.sched.tick_once()
    assert fired == 0
    assert received == []


async def test_scheduler_skips_future_reminder(env):
    """A reminder whose fire_at is in the future is left alone."""
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    event = await env.svc.create_event(
        space_id="sp-rem",
        summary="Later",
        start=future.isoformat(),
        end=future.isoformat(),
        created_by="uid-alice",
    )
    await env.svc.add_reminder(
        event_id=event.id, user_id="uid-alice", minutes_before=30,
    )
    fired = await env.sched.tick_once()
    assert fired == 0

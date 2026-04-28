"""Tests for socialhome.services.calendar_service."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.calendar import CalendarEvent, CalendarRSVP, RSVPStatus
from socialhome.repositories.calendar_repo import (
    SqliteCalendarRepo,
    SqliteSpaceCalendarRepo,
)
from socialhome.services.calendar_service import CalendarService


@pytest.fixture
async def env(tmp_dir):
    """Env with calendar repos and service over a real SQLite database."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.cal_repo = SqliteCalendarRepo(db)
    e.space_cal_repo = SqliteSpaceCalendarRepo(db)
    e.cal_svc = CalendarService(e.cal_repo)
    yield e
    await db.shutdown()


async def test_personal_calendar_crud(env):
    """Create calendar, add event, query by range, delete."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "uid-anna", "Anna"),
    )
    cal = await env.cal_svc.create_calendar(
        name="Personal", owner_username="anna", color="#FF0000"
    )
    assert cal.name == "Personal"

    now = datetime.now(timezone.utc)
    event = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Lunch",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
    )
    assert event.summary == "Lunch"

    events = await env.cal_svc.list_events_in_range(
        cal.id,
        start=(now - timedelta(minutes=30)).isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
    )
    assert len(events) == 1

    no_events = await env.cal_svc.list_events_in_range(
        cal.id,
        start=(now - timedelta(hours=3)).isoformat(),
        end=(now - timedelta(hours=2)).isoformat(),
    )
    assert len(no_events) == 0

    await env.cal_svc.delete_event(event.id)
    with pytest.raises(KeyError):
        await env.cal_svc.get_event(event.id)

    await env.cal_svc.delete_calendar(cal.id)
    with pytest.raises(KeyError):
        await env.cal_svc.get_calendar(cal.id)


async def test_space_calendar_with_rsvps(env):
    """Space calendar event with RSVP going/decline/remove flow."""
    now = datetime.now(timezone.utc)

    kp = generate_identity_keypair()
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("owner1", "uid-owner1", "Owner"),
    )
    await env.db.enqueue(
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("space-1", "TestSpace", env.iid, "owner1", kp.public_key.hex()),
    )

    event = CalendarEvent(
        id=uuid.uuid4().hex,
        calendar_id="space-1",
        summary="Team meeting",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u1",
    )
    await env.space_cal_repo.save_event("space-1", event)

    rsvp_going = CalendarRSVP(
        event_id=event.id,
        user_id="u1",
        status=RSVPStatus.GOING,
        updated_at=now.isoformat(),
        occurrence_at=now.isoformat(),
    )
    await env.space_cal_repo.upsert_rsvp(rsvp_going)
    rsvps = await env.space_cal_repo.list_rsvps(event.id)
    assert len(rsvps) == 1
    assert rsvps[0].status == RSVPStatus.GOING

    rsvp_declined = CalendarRSVP(
        event_id=event.id,
        user_id="u1",
        status=RSVPStatus.DECLINED,
        updated_at=now.isoformat(),
        occurrence_at=now.isoformat(),
    )
    await env.space_cal_repo.upsert_rsvp(rsvp_declined)
    rsvps2 = await env.space_cal_repo.list_rsvps(event.id)
    assert rsvps2[0].status == RSVPStatus.DECLINED

    await env.space_cal_repo.remove_rsvp(
        event.id,
        "u1",
        occurrence_at=now.isoformat(),
    )
    rsvps3 = await env.space_cal_repo.list_rsvps(event.id)
    assert len(rsvps3) == 0


async def test_list_events_in_range(env):
    """list_events_in_range returns events within the given time window."""
    await env.db.enqueue(
        "INSERT INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("anna", "u1", "A"),
    )
    cal = await env.cal_svc.create_calendar(
        name="W", owner_username="anna", color="#00F"
    )
    now = datetime.now(timezone.utc)
    await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="E",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="u1",
    )
    events = await env.cal_svc.list_events_in_range(
        cal.id,
        start=(now - timedelta(hours=1)).isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
    )
    assert len(events) >= 1


async def test_create_calendar_empty_name_rejected(env):
    """Empty calendar name raises ValueError."""
    with pytest.raises(ValueError, match="empty"):
        await env.cal_svc.create_calendar(name="  ", owner_username="x")


async def test_get_nonexistent_calendar(env):
    """Getting a nonexistent calendar raises KeyError."""
    with pytest.raises(KeyError):
        await env.cal_svc.get_calendar("nonexistent")


async def test_delete_nonexistent_calendar(env):
    """Deleting a nonexistent calendar raises KeyError."""
    with pytest.raises(KeyError):
        await env.cal_svc.delete_calendar("nonexistent")


# ── SpaceCalendarService — per-occurrence + federation (Phase A) ────────────


@pytest.fixture
async def space_cal_env(env):
    """env + a SpaceCalendarService with a seeded space."""
    from socialhome.services.calendar_service import SpaceCalendarService

    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )
    kp = generate_identity_keypair()
    await env.db.enqueue(
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("sp-cal", "TestSpace", env.iid, "alice", kp.public_key.hex()),
    )
    from socialhome.infrastructure.event_bus import EventBus

    env.bus = EventBus()
    env.space_cal_svc = SpaceCalendarService(env.space_cal_repo, env.bus)
    yield env


async def test_rsvp_non_recurring_defaults_occurrence_to_event_start(space_cal_env):
    """RSVP without occurrence_at on a non-recurring event → uses event.start."""
    env = space_cal_env
    now = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Birthday",
        start=now.isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.GOING,
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    assert len(rsvps) == 1
    # occurrence_at should equal the event's start
    assert rsvps[0].occurrence_at == now.isoformat()


async def test_rsvp_recurring_requires_occurrence_at(space_cal_env):
    """RSVP without occurrence_at on a recurring event → ValueError."""
    env = space_cal_env
    seed = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Weekly standup",
        start=seed.isoformat(),
        end=(seed + timedelta(minutes=30)).isoformat(),
        created_by="uid-alice",
        rrule="FREQ=WEEKLY;COUNT=4",
    )
    with pytest.raises(ValueError, match="occurrence_at"):
        await env.space_cal_svc.rsvp(
            event_id=event.id,
            user_id="uid-alice",
            status=RSVPStatus.GOING,
        )


async def test_rsvp_recurring_rejects_invalid_occurrence(space_cal_env):
    """RSVP with occurrence_at that doesn't match the rrule → ValueError."""
    env = space_cal_env
    seed = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Weekly standup",
        start=seed.isoformat(),
        end=(seed + timedelta(minutes=30)).isoformat(),
        created_by="uid-alice",
        rrule="FREQ=WEEKLY;COUNT=4",
    )
    bad_occ = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)  # Tuesday, not Monday
    with pytest.raises(ValueError, match="not a valid occurrence"):
        await env.space_cal_svc.rsvp(
            event_id=event.id,
            user_id="uid-alice",
            status=RSVPStatus.GOING,
            occurrence_at=bad_occ,
        )


async def test_rsvp_recurring_separate_occurrences(space_cal_env):
    """RSVPs on two different occurrences yield two distinct rows."""
    env = space_cal_env
    seed = datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Weekly standup",
        start=seed.isoformat(),
        end=(seed + timedelta(minutes=30)).isoformat(),
        created_by="uid-alice",
        rrule="FREQ=WEEKLY;COUNT=4",
    )
    occ1 = seed
    occ2 = seed + timedelta(weeks=1)
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.GOING,
        occurrence_at=occ1,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.DECLINED,
        occurrence_at=occ2,
    )
    by_occ1 = await env.space_cal_svc.list_rsvps(event.id, occurrence_at=occ1)
    by_occ2 = await env.space_cal_svc.list_rsvps(event.id, occurrence_at=occ2)
    assert len(by_occ1) == 1 and by_occ1[0].status == RSVPStatus.GOING
    assert len(by_occ2) == 1 and by_occ2[0].status == RSVPStatus.DECLINED


async def test_rsvp_status_must_be_user_settable(space_cal_env):
    """User-driven RSVP can't set host-controlled statuses (requested/waitlist)."""
    env = space_cal_env
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Birthday",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    with pytest.raises(ValueError, match="must be one of"):
        await env.space_cal_svc.rsvp(
            event_id=event.id,
            user_id="uid-alice",
            status=RSVPStatus.WAITLIST,
        )


# ── Phase C: capacity + request-to-join + waitlist ─────────────────────────


async def test_create_event_auto_rsvps_creator_as_going(space_cal_env):
    env = space_cal_env
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Birthday party",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    assert len(rsvps) == 1
    assert rsvps[0].user_id == "uid-alice"
    assert rsvps[0].status == RSVPStatus.GOING


async def test_capped_event_member_rsvp_becomes_requested(space_cal_env):
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=5,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.REQUESTED


async def test_creator_skips_approval_even_when_capped(space_cal_env):
    env = space_cal_env
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=2,
    )
    # Creator's auto-RSVP from create_event lands as GOING.
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    alice = [r for r in rsvps if r.user_id == "uid-alice"][0]
    assert alice.status == RSVPStatus.GOING


async def test_approve_promotes_requested_to_going(space_cal_env):
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=5,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    new_status = await env.space_cal_svc.approve_rsvp(
        event_id=event.id,
        user_id="uid-bob",
    )
    assert new_status == RSVPStatus.GOING
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.GOING


async def test_approve_lands_on_waitlist_when_full(space_cal_env):
    env = space_cal_env
    for u in ("bob", "carol"):
        await env.db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (u, f"uid-{u}", u.title()),
        )
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Tiny event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=1,
    )
    # Capacity is 1, alice already takes the seat.
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    bob_status = await env.space_cal_svc.approve_rsvp(
        event_id=event.id,
        user_id="uid-bob",
    )
    assert bob_status == RSVPStatus.WAITLIST


async def test_decline_promotes_waitlist(space_cal_env):
    env = space_cal_env
    for u in ("bob", "carol"):
        await env.db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (u, f"uid-{u}", u.title()),
        )
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Limited",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=1,
    )
    # bob requests, gets waitlisted on approval.
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    await env.space_cal_svc.approve_rsvp(event_id=event.id, user_id="uid-bob")
    # alice declines (gives up her seat) — bob should auto-promote.
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.DECLINED,
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.GOING


async def test_deny_removes_request(space_cal_env):
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=10,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    await env.space_cal_svc.deny_rsvp(event_id=event.id, user_id="uid-bob")
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bobs = [r for r in rsvps if r.user_id == "uid-bob"]
    assert bobs == []


async def test_list_pending_only_returns_requested(space_cal_env):
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=2,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    pending = await env.space_cal_svc.list_pending(event.id)
    assert len(pending) == 1
    assert pending[0].user_id == "uid-bob"
    assert pending[0].status == RSVPStatus.REQUESTED


async def test_capacity_raise_promotes_waitlist(space_cal_env):
    env = space_cal_env
    for u in ("bob", "carol"):
        await env.db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (u, f"uid-{u}", u.title()),
        )
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=1,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    await env.space_cal_svc.approve_rsvp(event_id=event.id, user_id="uid-bob")
    # bob is waitlisted (alice has the only seat).
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    assert any(
        r.user_id == "uid-bob" and r.status == RSVPStatus.WAITLIST for r in rsvps
    )
    # Raise capacity — bob should promote.
    await env.space_cal_svc.update_event(event.id, capacity=2)
    rsvps2 = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps2 if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.GOING


async def test_uncapped_event_keeps_old_behaviour(space_cal_env):
    """No capacity → "going" is direct, no approval flow."""
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Open event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.GOING


async def test_rsvp_to_ended_event_rejected(space_cal_env):
    """Phase E: RSVPs to occurrences whose window is fully in the past
    are rejected at the service layer."""
    env = space_cal_env
    past = datetime.now(timezone.utc) - timedelta(days=1)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Past event",
        start=past.isoformat(),
        end=(past + timedelta(hours=1)).isoformat(),
        created_by="uid-alice",
    )
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    with pytest.raises(ValueError, match="already ended"):
        await env.space_cal_svc.rsvp(
            event_id=event.id,
            user_id="uid-bob",
            status=RSVPStatus.GOING,
        )


async def test_rsvp_during_event_window_allowed(space_cal_env):
    """While an event is happening (started but not ended), RSVPs go through."""
    env = space_cal_env
    now = datetime.now(timezone.utc)
    # Event that started 30 min ago and lasts 2 h.
    started = now - timedelta(minutes=30)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Currently happening",
        start=started.isoformat(),
        end=(now + timedelta(hours=1, minutes=30)).isoformat(),
        created_by="uid-alice",
    )
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    # Should NOT raise.
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )


async def test_negative_capacity_rejected(space_cal_env):
    env = space_cal_env
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="capacity"):
        await env.space_cal_svc.create_event(
            space_id="sp-cal",
            summary="Bad",
            start=now.isoformat(),
            end=now.isoformat(),
            created_by="uid-alice",
            capacity=-1,
        )


async def test_member_left_cleans_up_rsvps(space_cal_env):
    """Phase E: SpaceMemberLeft subscriber drops the user's RSVPs in the space."""
    from socialhome.domain.events import SpaceMemberLeft

    env = space_cal_env
    env.space_cal_svc.wire()
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    future = datetime.now(timezone.utc) + timedelta(days=10)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Anniversary",
        start=future.isoformat(),
        end=(future + timedelta(hours=1)).isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    rsvps_before = await env.space_cal_svc.list_rsvps(event.id)
    assert any(r.user_id == "uid-bob" for r in rsvps_before)
    # Bob leaves the space.
    await env.bus.publish(SpaceMemberLeft(space_id="sp-cal", user_id="uid-bob"))
    rsvps_after = await env.space_cal_svc.list_rsvps(event.id)
    assert not any(r.user_id == "uid-bob" for r in rsvps_after)
    # Alice (the creator) is still RSVPed.
    assert any(r.user_id == "uid-alice" for r in rsvps_after)


async def test_rsvp_publishes_federation_event(space_cal_env):
    """rsvp() calls broadcast_to_space_members on the federation service."""
    env = space_cal_env

    class _FakeFed:
        def __init__(self):
            self.calls: list[tuple] = []

        async def broadcast_to_space_members(self, space_id, event_type, payload):
            self.calls.append((space_id, event_type, payload))

    fed = _FakeFed()
    env.space_cal_svc.attach_federation(fed)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Anniversary",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.GOING,
    )
    # remove_rsvp also fires
    await env.space_cal_svc.remove_rsvp(
        event_id=event.id,
        user_id="uid-alice",
    )
    assert len(fed.calls) == 2
    assert fed.calls[0][0] == "sp-cal"
    assert fed.calls[0][1].value == "space_rsvp_updated"
    assert fed.calls[0][2]["status"] == RSVPStatus.GOING
    assert fed.calls[0][2]["occurrence_at"] == now.isoformat()
    assert fed.calls[1][1].value == "space_rsvp_deleted"
    assert "status" not in fed.calls[1][2]


async def test_create_event_empty_summary(env):
    """Empty event summary raises ValueError."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("bob", "u2", "B"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="bob")
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="empty"):
        await env.cal_svc.create_event(
            calendar_id=cal.id,
            summary="  ",
            start=now.isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            created_by="u2",
        )


async def test_create_event_nonexistent_calendar(env):
    """Creating an event in a nonexistent calendar raises KeyError."""
    now = datetime.now(timezone.utc)
    with pytest.raises(KeyError):
        await env.cal_svc.create_event(
            calendar_id="nonexistent",
            summary="X",
            start=now.isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            created_by="u1",
        )


async def test_create_event_end_before_start(env):
    """Event with end < start raises ValueError."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("carl", "u3", "C"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="carl")
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="before start"):
        await env.cal_svc.create_event(
            calendar_id=cal.id,
            summary="Bad",
            start=(now + timedelta(hours=2)).isoformat(),
            end=now.isoformat(),
            created_by="u3",
        )


async def test_create_event_invalid_datetime(env):
    """Invalid datetime string raises ValueError."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("dan", "u4", "D"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="dan")
    with pytest.raises(ValueError, match="invalid datetime"):
        await env.cal_svc.create_event(
            calendar_id=cal.id,
            summary="X",
            start="not-a-date",
            end="also-not",
            created_by="u4",
        )


async def test_get_nonexistent_event(env):
    """Getting a nonexistent event raises KeyError."""
    with pytest.raises(KeyError):
        await env.cal_svc.get_event("nonexistent")


async def test_delete_nonexistent_event(env):
    """Deleting a nonexistent event raises KeyError."""
    with pytest.raises(KeyError):
        await env.cal_svc.delete_event("nonexistent")


async def test_list_calendars(env):
    """list_calendars returns calendars for the given user."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("eve", "u5", "E"),
    )
    await env.cal_svc.create_calendar(name="C1", owner_username="eve")
    cals = await env.cal_svc.list_calendars("eve")
    assert len(cals) >= 1


async def test_space_calendar_service_list(env):
    """SpaceCalendarService.list_events_in_range works."""
    from socialhome.services.calendar_service import SpaceCalendarService

    svc = SpaceCalendarService(env.space_cal_repo)
    # Need a space
    kp2 = generate_identity_keypair()
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("spown", "uid-sp", "SP"),
    )
    sid = uuid.uuid4().hex
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode)
           VALUES(?,?,?,?,?,0,'private','invite_only')""",
        (sid, "SpCal", env.iid, "spown", kp2.public_key.hex()),
    )
    now = datetime.now(timezone.utc)
    events = await svc.list_events_in_range(
        sid,
        start=(now - timedelta(hours=1)).isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
    )
    assert isinstance(events, list)


# ─── CalendarService publishes domain events (B1) ─────────────────────


async def _seed_user(db, username="owner"):
    await db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, f"uid-{username}", username),
    )


async def test_create_event_publishes_calendar_event_created(env):
    """CalendarService.create_event publishes CalendarEventCreated on the bus."""
    from socialhome.domain.events import CalendarEventCreated

    class _RecordingBus:
        def __init__(self):
            self.events = []

        def subscribe(self, *a, **kw):
            pass

        async def publish(self, event):
            self.events.append(event)

    await _seed_user(env.db)
    bus = _RecordingBus()
    svc = CalendarService(env.cal_repo, bus=bus)
    cal = await svc.create_calendar(name="Test", owner_username="owner")
    now = datetime.now(timezone.utc)
    await svc.create_event(
        calendar_id=cal.id,
        summary="Dinner",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-owner",
    )
    assert any(isinstance(e, CalendarEventCreated) for e in bus.events)


async def test_delete_event_publishes_calendar_event_deleted(env):
    """CalendarService.delete_event publishes CalendarEventDeleted on the bus."""
    from socialhome.domain.events import CalendarEventDeleted

    class _RecordingBus:
        def __init__(self):
            self.events = []

        def subscribe(self, *a, **kw):
            pass

        async def publish(self, event):
            self.events.append(event)

    await _seed_user(env.db, "deleter")
    bus = _RecordingBus()
    svc = CalendarService(env.cal_repo, bus=bus)
    cal = await svc.create_calendar(name="Del", owner_username="deleter")
    now = datetime.now(timezone.utc)
    event = await svc.create_event(
        calendar_id=cal.id,
        summary="To delete",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-deleter",
    )
    await svc.delete_event(event.id)
    assert any(isinstance(e, CalendarEventDeleted) for e in bus.events)

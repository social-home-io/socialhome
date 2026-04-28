"""Tests for SqliteCalendarRepo and SqliteSpaceCalendarRepo."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.calendar import (
    Calendar,
    CalendarEvent,
    CalendarRSVP,
    RSVPStatus,
)
from socialhome.repositories.calendar_repo import (
    SqliteCalendarRepo,
    SqliteSpaceCalendarRepo,
)


@pytest.fixture
async def env(tmp_dir):
    """Env with calendar repos over a real SQLite database."""
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
    # Seed a user for FK constraints on calendar owner
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.cal_repo = SqliteCalendarRepo(db)
    e.space_cal_repo = SqliteSpaceCalendarRepo(db)
    yield e
    await db.shutdown()


# ── Personal calendars ──────────────────────────────────────────────────────


async def test_save_and_get_calendar(env):
    """save_calendar persists a calendar; get_calendar retrieves it."""
    cal = Calendar(id="cal-1", name="Personal", color="#fff", owner_username="alice")
    saved = await env.cal_repo.save_calendar(cal)
    assert saved.id == "cal-1"
    fetched = await env.cal_repo.get_calendar("cal-1")
    assert fetched is not None
    assert fetched.name == "Personal"


async def test_get_calendar_missing(env):
    """get_calendar returns None for an unknown id."""
    result = await env.cal_repo.get_calendar("no-such-cal")
    assert result is None


async def test_list_calendars_for_user(env):
    """list_calendars_for_user returns all calendars owned by the user."""
    cal1 = Calendar(id="c1", name="A", color="#aaa", owner_username="alice")
    cal2 = Calendar(id="c2", name="B", color="#bbb", owner_username="alice")
    await env.cal_repo.save_calendar(cal1)
    await env.cal_repo.save_calendar(cal2)
    result = await env.cal_repo.list_calendars_for_user("alice")
    assert len(result) == 2


async def test_save_calendar_upserts(env):
    """save_calendar with the same id updates the existing record."""
    cal = Calendar(id="cal-up", name="Old Name", color="#000", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    updated = Calendar(
        id="cal-up", name="New Name", color="#fff", owner_username="alice"
    )
    await env.cal_repo.save_calendar(updated)
    fetched = await env.cal_repo.get_calendar("cal-up")
    assert fetched.name == "New Name"


async def test_delete_calendar(env):
    """delete_calendar removes the calendar."""
    cal = Calendar(id="cal-del", name="Gone", color="#000", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    await env.cal_repo.delete_calendar("cal-del")
    assert await env.cal_repo.get_calendar("cal-del") is None


# ── Personal calendar events ────────────────────────────────────────────────


async def test_save_and_get_event(env):
    """save_event persists an event; get_event retrieves it."""
    cal = Calendar(id="cal-ev", name="Events", color="#fff", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    now = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
    end = datetime(2025, 6, 1, 11, 0, tzinfo=timezone.utc)
    evt = CalendarEvent(
        id="ev-1",
        calendar_id="cal-ev",
        summary="Standup",
        start=now,
        end=end,
        created_by="uid-alice",
    )
    await env.cal_repo.save_event(evt)
    fetched = await env.cal_repo.get_event("ev-1")
    assert fetched is not None
    assert fetched.summary == "Standup"


async def test_get_event_missing(env):
    """get_event returns None for an unknown event id."""
    assert await env.cal_repo.get_event("nope") is None


async def test_list_events_in_range(env):
    """list_events_in_range returns events that overlap the time window."""
    cal = Calendar(id="cal-r", name="R", color="#fff", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    start1 = datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc)
    end1 = datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)
    start2 = datetime(2025, 6, 2, 8, 0, tzinfo=timezone.utc)
    end2 = datetime(2025, 6, 2, 9, 0, tzinfo=timezone.utc)
    evt1 = CalendarEvent(
        id="ev-r1",
        calendar_id="cal-r",
        summary="E1",
        start=start1,
        end=end1,
        created_by="uid-alice",
    )
    evt2 = CalendarEvent(
        id="ev-r2",
        calendar_id="cal-r",
        summary="E2",
        start=start2,
        end=end2,
        created_by="uid-alice",
    )
    await env.cal_repo.save_event(evt1)
    await env.cal_repo.save_event(evt2)
    window_start = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
    window_end = datetime(2025, 6, 1, 23, 59, tzinfo=timezone.utc)
    results = await env.cal_repo.list_events_in_range(
        "cal-r", start=window_start, end=window_end
    )
    assert len(results) == 1
    assert results[0].summary == "E1"


async def test_delete_event(env):
    """delete_event removes the event from the database."""
    cal = Calendar(id="cal-de", name="X", color="#fff", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    now = datetime(2025, 6, 1, tzinfo=timezone.utc)
    evt = CalendarEvent(
        id="ev-del",
        calendar_id="cal-de",
        summary="Bye",
        start=now,
        end=now,
        created_by="uid-alice",
    )
    await env.cal_repo.save_event(evt)
    await env.cal_repo.delete_event("ev-del")
    assert await env.cal_repo.get_event("ev-del") is None


# ── Space calendar events ───────────────────────────────────────────────────


async def _seed_space(env, space_id: str = "sp-1") -> str:
    """Insert a minimal space row for FK constraints."""
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username, identity_public_key)
           VALUES(?,?,?,?,?)""",
        (space_id, "TestSpace", "inst-x", "alice", "aabb" * 16),
    )
    return space_id


async def test_space_cal_save_and_get(env):
    """save_event on space calendar persists; get_event retrieves (space_id, event)."""
    sid = await _seed_space(env)
    now = datetime(2025, 7, 1, tzinfo=timezone.utc)
    end = datetime(2025, 7, 1, 1, tzinfo=timezone.utc)
    evt = CalendarEvent(
        id="sp-ev-1",
        calendar_id=sid,
        summary="Space Event",
        start=now,
        end=end,
        created_by="uid-alice",
    )
    await env.space_cal_repo.save_event(sid, evt)
    result = await env.space_cal_repo.get_event("sp-ev-1")
    assert result is not None
    returned_sid, returned_evt = result
    assert returned_sid == sid
    assert returned_evt.summary == "Space Event"


async def test_space_cal_list_events_in_range(env):
    """list_events_in_range for space calendar filters by time window."""
    sid = await _seed_space(env, "sp-2")
    s1 = datetime(2025, 8, 1, tzinfo=timezone.utc)
    e1 = datetime(2025, 8, 1, 1, tzinfo=timezone.utc)
    s2 = datetime(2025, 8, 10, tzinfo=timezone.utc)
    e2 = datetime(2025, 8, 10, 1, tzinfo=timezone.utc)
    ev1 = CalendarEvent(
        id="sp-ev-a",
        calendar_id=sid,
        summary="A",
        start=s1,
        end=e1,
        created_by="uid-alice",
    )
    ev2 = CalendarEvent(
        id="sp-ev-b",
        calendar_id=sid,
        summary="B",
        start=s2,
        end=e2,
        created_by="uid-alice",
    )
    await env.space_cal_repo.save_event(sid, ev1)
    await env.space_cal_repo.save_event(sid, ev2)
    ws = datetime(2025, 8, 1, tzinfo=timezone.utc)
    we = datetime(2025, 8, 5, tzinfo=timezone.utc)
    results = await env.space_cal_repo.list_events_in_range(sid, start=ws, end=we)
    assert len(results) == 1
    assert results[0].summary == "A"


async def test_space_cal_rsvp_upsert_and_list(env):
    """upsert_rsvp stores an RSVP; list_rsvps retrieves it."""
    sid = await _seed_space(env, "sp-3")
    now = datetime(2025, 9, 1, tzinfo=timezone.utc)
    end = datetime(2025, 9, 1, 1, tzinfo=timezone.utc)
    evt = CalendarEvent(
        id="sp-ev-r",
        calendar_id=sid,
        summary="Party",
        start=now,
        end=end,
        created_by="uid-alice",
    )
    await env.space_cal_repo.save_event(sid, evt)
    rsvp = CalendarRSVP(
        event_id="sp-ev-r",
        user_id="uid-alice",
        status=RSVPStatus.GOING,
        updated_at="2025-09-01T00:00:00",
        occurrence_at=now.isoformat(),
    )
    await env.space_cal_repo.upsert_rsvp(rsvp)
    rsvps = await env.space_cal_repo.list_rsvps("sp-ev-r")
    assert len(rsvps) == 1
    assert rsvps[0].status == RSVPStatus.GOING
    assert rsvps[0].occurrence_at == now.isoformat()


async def test_space_cal_rsvp_upsert_update(env):
    """upsert_rsvp with the same (event_id, user_id) updates the status."""
    sid = await _seed_space(env, "sp-4")
    now = datetime(2025, 9, 1, tzinfo=timezone.utc)
    evt = CalendarEvent(
        id="sp-ev-u",
        calendar_id=sid,
        summary="Party2",
        start=now,
        end=now,
        created_by="uid-alice",
    )
    await env.space_cal_repo.save_event(sid, evt)
    rsvp1 = CalendarRSVP(
        event_id="sp-ev-u",
        user_id="uid-alice",
        status=RSVPStatus.GOING,
        updated_at="2025-09-01T00:00:00",
        occurrence_at=now.isoformat(),
    )
    rsvp2 = CalendarRSVP(
        event_id="sp-ev-u",
        user_id="uid-alice",
        status=RSVPStatus.MAYBE,
        updated_at="2025-09-02T00:00:00",
        occurrence_at=now.isoformat(),
    )
    await env.space_cal_repo.upsert_rsvp(rsvp1)
    await env.space_cal_repo.upsert_rsvp(rsvp2)
    rsvps = await env.space_cal_repo.list_rsvps("sp-ev-u")
    assert len(rsvps) == 1
    assert rsvps[0].status == RSVPStatus.MAYBE


async def test_space_cal_rsvp_invalid_status_raises(env):
    """upsert_rsvp raises ValueError for an invalid status string."""
    rsvp = CalendarRSVP(
        event_id="x",
        user_id="u",
        status="maybe-not",
        updated_at="now",
        occurrence_at="2025-09-01T00:00:00",
    )
    with pytest.raises(ValueError, match="invalid RSVP status"):
        await env.space_cal_repo.upsert_rsvp(rsvp)


async def test_recurring_event_expands_into_window(env):
    """A DAILY rrule yields one virtual event per day in the window."""
    cal = Calendar(id="cal-rr", name="Recurring", color="#fff", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    seed = CalendarEvent(
        id="ev-daily",
        calendar_id="cal-rr",
        summary="Daily standup",
        start=datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 6, 9, 30, tzinfo=timezone.utc),
        created_by="uid-alice",
        rrule="FREQ=DAILY;COUNT=5",
    )
    await env.cal_repo.save_event(seed)
    events = await env.cal_repo.list_events_in_range(
        "cal-rr",
        start=datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc),
    )
    # COUNT=5 → 5 occurrences in window.
    assert len(events) == 5
    days = sorted({e.start.day for e in events})
    assert days == [6, 7, 8, 9, 10]
    # Seed round-trip preserves rrule.
    persisted = await env.cal_repo.get_event("ev-daily")
    assert persisted is not None
    assert persisted.rrule == "FREQ=DAILY;COUNT=5"


async def test_non_recurring_event_still_works(env):
    cal = Calendar(id="cal-nr", name="One-off", color="#fff", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    seed = CalendarEvent(
        id="ev-once",
        calendar_id="cal-nr",
        summary="Dentist",
        start=datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 4, 6, 9, 30, tzinfo=timezone.utc),
        created_by="uid-alice",
    )
    await env.cal_repo.save_event(seed)
    events = await env.cal_repo.list_events_in_range(
        "cal-nr",
        start=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert len(events) == 1
    assert events[0].id == "ev-once"
    assert events[0].rrule is None


async def test_space_cal_rsvp_remove(env):
    """remove_rsvp deletes the RSVP record."""
    sid = await _seed_space(env, "sp-5")
    now = datetime(2025, 9, 1, tzinfo=timezone.utc)
    evt = CalendarEvent(
        id="sp-ev-rm",
        calendar_id=sid,
        summary="Test",
        start=now,
        end=now,
        created_by="uid-alice",
    )
    await env.space_cal_repo.save_event(sid, evt)
    rsvp = CalendarRSVP(
        event_id="sp-ev-rm",
        user_id="uid-alice",
        status=RSVPStatus.GOING,
        updated_at="2025-09-01T00:00:00",
        occurrence_at=now.isoformat(),
    )
    await env.space_cal_repo.upsert_rsvp(rsvp)
    await env.space_cal_repo.remove_rsvp(
        "sp-ev-rm",
        "uid-alice",
        occurrence_at=now.isoformat(),
    )
    rsvps = await env.space_cal_repo.list_rsvps("sp-ev-rm")
    assert rsvps == []


# ── Per-occurrence RSVPs (Phase A) ───────────────────────────────────────────


async def test_rsvp_per_occurrence_distinct_rows(env):
    """A recurring event keeps RSVPs per (user, occurrence)."""
    sid = await _seed_space(env, "sp-occ")
    seed = datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc)
    evt = CalendarEvent(
        id="sp-ev-occ",
        calendar_id=sid,
        summary="Weekly standup",
        start=seed,
        end=seed,
        created_by="uid-alice",
        rrule="FREQ=WEEKLY;COUNT=3",
    )
    await env.space_cal_repo.save_event(sid, evt)
    occ1 = seed.isoformat()
    occ2 = (datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc)).isoformat()
    await env.space_cal_repo.upsert_rsvp(
        CalendarRSVP(
            event_id="sp-ev-occ",
            user_id="uid-alice",
            status=RSVPStatus.GOING,
            updated_at="2026-05-01T00:00:00",
            occurrence_at=occ1,
        )
    )
    await env.space_cal_repo.upsert_rsvp(
        CalendarRSVP(
            event_id="sp-ev-occ",
            user_id="uid-alice",
            status=RSVPStatus.DECLINED,
            updated_at="2026-05-08T00:00:00",
            occurrence_at=occ2,
        )
    )
    all_rsvps = await env.space_cal_repo.list_rsvps("sp-ev-occ")
    assert len(all_rsvps) == 2
    week1_only = await env.space_cal_repo.list_rsvps(
        "sp-ev-occ",
        occurrence_at=occ1,
    )
    assert len(week1_only) == 1
    assert week1_only[0].status == RSVPStatus.GOING
    week2_only = await env.space_cal_repo.list_rsvps(
        "sp-ev-occ",
        occurrence_at=occ2,
    )
    assert len(week2_only) == 1
    assert week2_only[0].status == RSVPStatus.DECLINED


async def test_rsvp_buffer_holds_orphan_rsvps(env):
    """RSVP that arrives before its event is buffered until the event lands."""
    occ_iso = "2026-06-01T18:00:00+00:00"
    await env.space_cal_repo.buffer_pending_rsvp(
        event_id="ev-future",
        user_id="uid-bob",
        occurrence_at=occ_iso,
        status=RSVPStatus.GOING,
        updated_at="2026-05-20T00:00:00",
    )
    # No event yet → no live RSVP rows.
    assert await env.space_cal_repo.list_rsvps("ev-future") == []
    # Event arrives — flush picks up the buffered RSVP.
    sid = await _seed_space(env, "sp-future")
    seed = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
    evt = CalendarEvent(
        id="ev-future",
        calendar_id=sid,
        summary="Game night",
        start=seed,
        end=seed,
        created_by="uid-alice",
    )
    await env.space_cal_repo.save_event(sid, evt)
    applied = await env.space_cal_repo.flush_pending_rsvps("ev-future")
    assert len(applied) == 1
    assert applied[0].user_id == "uid-bob"
    rsvps = await env.space_cal_repo.list_rsvps("ev-future")
    assert len(rsvps) == 1
    assert rsvps[0].status == RSVPStatus.GOING
    # Buffer is drained.
    re_flush = await env.space_cal_repo.flush_pending_rsvps("ev-future")
    assert re_flush == []


async def test_rsvp_buffer_removed_status_drops_live_row(env):
    """If a 'removed' RSVP buffers, then the event arrives, the live row stays gone."""
    sid = await _seed_space(env, "sp-rm-buf")
    seed = datetime(2026, 6, 5, tzinfo=timezone.utc)
    occ_iso = seed.isoformat()
    # Event exists locally, RSVP exists.
    evt = CalendarEvent(
        id="ev-rm",
        calendar_id=sid,
        summary="Dinner",
        start=seed,
        end=seed,
        created_by="uid-alice",
    )
    await env.space_cal_repo.save_event(sid, evt)
    await env.space_cal_repo.upsert_rsvp(
        CalendarRSVP(
            event_id="ev-rm",
            user_id="uid-bob",
            status=RSVPStatus.GOING,
            updated_at="2026-05-30T00:00:00",
            occurrence_at=occ_iso,
        )
    )
    # A buffered 'removed' arrives — flush should delete the live row.
    await env.space_cal_repo.buffer_pending_rsvp(
        event_id="ev-rm",
        user_id="uid-bob",
        occurrence_at=occ_iso,
        status="removed",
        updated_at="2026-06-01T00:00:00",
    )
    await env.space_cal_repo.flush_pending_rsvps("ev-rm")
    assert await env.space_cal_repo.list_rsvps("ev-rm") == []


async def test_rsvp_buffer_gc_drops_old_rows(env):
    """gc_pending_rsvps purges rows older than the cutoff."""
    # Stuff a row whose received_at is in the past via raw SQL — the
    # public API only writes 'now()'.
    await env.db.enqueue(
        """
        INSERT INTO pending_federated_rsvps(
            event_id, user_id, occurrence_at, status, updated_at, received_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            "ev-old",
            "uid-x",
            "2024-01-01T00:00:00",
            "going",
            "2024-01-01T00:00:00",
            "2024-01-01T00:00:00",
        ),
    )
    n = await env.space_cal_repo.gc_pending_rsvps(
        older_than_iso="2025-01-01T00:00:00",
    )
    assert n == 1

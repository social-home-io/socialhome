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
    _chunks,
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


# ── Personal-calendar RSVPs + remote-invite columns (§23.60) ───────────────


async def test_personal_calendar_rsvp_upsert_and_list(env):
    """Sqlite path for upsert_rsvp + list_rsvps. Mirrors the
    space_calendar_rsvps shape but on the personal table."""
    cal = Calendar(id="c1", name="Anna", color="#abc", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    now = datetime.now(timezone.utc)
    ev = CalendarEvent(
        id="evt-1",
        calendar_id="c1",
        summary="Garden party",
        start=now,
        end=now,
        created_by="uid-alice",
    )
    await env.cal_repo.save_event(ev)
    await env.cal_repo.upsert_rsvp(
        CalendarRSVP(
            event_id="evt-1",
            user_id="u-bob",
            status="accepted",
            updated_at=now.isoformat(),
            occurrence_at=now.isoformat(),
        )
    )
    rsvps = await env.cal_repo.list_rsvps("evt-1")
    assert len(rsvps) == 1
    assert rsvps[0].status == "accepted"
    # Per-occurrence query also works.
    by_occ = await env.cal_repo.list_rsvps("evt-1", occurrence_at=now.isoformat())
    assert len(by_occ) == 1
    # Re-upsert with a new status overwrites in place.
    await env.cal_repo.upsert_rsvp(
        CalendarRSVP(
            event_id="evt-1",
            user_id="u-bob",
            status="declined",
            updated_at=now.isoformat(),
            occurrence_at=now.isoformat(),
        )
    )
    rsvps = await env.cal_repo.list_rsvps("evt-1")
    assert len(rsvps) == 1
    assert rsvps[0].status == "declined"


async def test_personal_calendar_rsvp_remove(env):
    cal = Calendar(id="c1", name="Anna", color="#abc", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    now = datetime.now(timezone.utc)
    ev = CalendarEvent(
        id="evt-1",
        calendar_id="c1",
        summary="P",
        start=now,
        end=now,
        created_by="uid-alice",
    )
    await env.cal_repo.save_event(ev)
    await env.cal_repo.upsert_rsvp(
        CalendarRSVP(
            event_id="evt-1",
            user_id="u-bob",
            status="accepted",
            updated_at=now.isoformat(),
            occurrence_at=now.isoformat(),
        )
    )
    # Remove with explicit occurrence_at.
    await env.cal_repo.remove_rsvp(
        "evt-1",
        "u-bob",
        occurrence_at=now.isoformat(),
    )
    assert await env.cal_repo.list_rsvps("evt-1") == []
    # Re-add then remove without occurrence_at — the "all rows for this
    # (event, user)" path.
    await env.cal_repo.upsert_rsvp(
        CalendarRSVP(
            event_id="evt-1",
            user_id="u-bob",
            status="accepted",
            updated_at=now.isoformat(),
            occurrence_at=now.isoformat(),
        )
    )
    await env.cal_repo.remove_rsvp("evt-1", "u-bob")
    assert await env.cal_repo.list_rsvps("evt-1") == []


async def test_personal_calendar_rsvp_upsert_rejects_empty_occurrence(env):
    """Service guarantees a non-empty occurrence_at — repo enforces
    so a buggy caller can't sneak past the PK constraint."""
    with pytest.raises(ValueError, match="occurrence_at"):
        await env.cal_repo.upsert_rsvp(
            CalendarRSVP(
                event_id="evt-1",
                user_id="u-bob",
                status="accepted",
                updated_at="2026-01-01T00:00:00",
                occurrence_at="",
            )
        )


async def test_get_event_by_remote_finds_remote_invite(env):
    """Sqlite path for get_event_by_remote — used by
    PersonalCalendarInboundHandlers to dedupe re-delivered envelopes.
    Also covers the round-trip of the new origin / remote_event_id /
    remote_instance_id columns through save_event."""
    cal = Calendar(id="c1", name="Anna", color="#abc", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    now = datetime.now(timezone.utc)
    invite = CalendarEvent(
        id="ri_local",
        calendar_id="c1",
        summary="Cross-household party",
        start=now,
        end=now,
        created_by="u-bob-remote",
        origin="remote_invite",
        remote_event_id="org-evt-1",
        remote_instance_id="i_org",
    )
    await env.cal_repo.save_event(invite)
    found = await env.cal_repo.get_event_by_remote(
        remote_instance_id="i_org",
        remote_event_id="org-evt-1",
    )
    assert found is not None
    assert found.id == "ri_local"
    assert found.origin == "remote_invite"
    assert found.remote_event_id == "org-evt-1"
    assert found.remote_instance_id == "i_org"
    # Miss returns None.
    miss = await env.cal_repo.get_event_by_remote(
        remote_instance_id="i_other",
        remote_event_id="ghost",
    )
    assert miss is None


async def test_save_event_origin_defaults_to_local(env):
    """Round-trip of a default-origin event still surfaces
    origin='local' (the schema default) without the caller setting it."""
    cal = Calendar(id="c1", name="Anna", color="#abc", owner_username="alice")
    await env.cal_repo.save_calendar(cal)
    now = datetime.now(timezone.utc)
    ev = CalendarEvent(
        id="evt-default",
        calendar_id="c1",
        summary="Local",
        start=now,
        end=now,
        created_by="uid-alice",
    )
    await env.cal_repo.save_event(ev)
    fetched = await env.cal_repo.get_event("evt-default")
    assert fetched is not None
    assert fetched.origin == "local"
    assert fetched.remote_event_id is None
    assert fetched.remote_instance_id is None


# ── §Audit #3 — feed tokens stored as SHA-256 hash ───────────────────────


async def test_feed_token_persisted_as_hash_not_plaintext(env):
    """The raw token returned to the user MUST NOT appear in storage —
    only its SHA-256 hash. Lookup hashes the inbound query string token
    before matching, so the round-trip works without leaking the secret."""
    from socialhome.auth import sha256_token_hash

    sid = await _seed_space(env, "sp-feed")
    raw_token = "user-visible-token-12345"

    await env.space_cal_repo.upsert_feed_token(
        user_id="uid-alice",
        space_id=sid,
        token=raw_token,
    )

    # Direct DB read — the column should hold the hash, not the raw token.
    row = await env.db.fetchone(
        "SELECT token_hash FROM space_calendar_feed_tokens WHERE user_id=? AND space_id=?",
        ("uid-alice", sid),
    )
    stored = row["token_hash"] if row else None
    assert stored == sha256_token_hash(raw_token)
    assert stored != raw_token

    # And lookup with the raw token still resolves the row.
    resolved = await env.space_cal_repo.get_user_for_feed_token(raw_token)
    assert resolved == ("uid-alice", sid)

    # A bogus token does not resolve.
    bogus = await env.space_cal_repo.get_user_for_feed_token("not-a-token")
    assert bogus is None


async def test_feed_token_revoked_lookup_returns_none(env):
    """Once revoked, the token no longer resolves even if the raw value
    was reused (the route returns 401)."""
    sid = await _seed_space(env, "sp-feed-rev")
    raw = "another-token"
    await env.space_cal_repo.upsert_feed_token(
        user_id="uid-alice",
        space_id=sid,
        token=raw,
    )
    await env.space_cal_repo.revoke_feed_token(
        user_id="uid-alice",
        space_id=sid,
    )
    assert await env.space_cal_repo.get_user_for_feed_token(raw) is None


# ── Shared-event copies (client_event_uuid fan-out) ─────────────────────────


async def _seed_user(env, username: str) -> None:
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, f"uid-{username}", username.title()),
    )


async def _seed_calendar(
    env,
    cal_id: str,
    owner: str,
    calendar_type: str = "personal",
) -> None:
    await env.cal_repo.save_calendar(
        Calendar(
            id=cal_id,
            name=cal_id,
            color="#abc",
            owner_username=owner,
            calendar_type=calendar_type,
        )
    )


async def _insert_event(
    env,
    *,
    event_id: str,
    calendar_id: str,
    client_event_uuid: str | None,
    origin: str = "local",
    mirrored_from: str | None = None,
    created_at: str = "2025-01-01 10:00:00",
) -> None:
    """Insert a raw calendar_events row.

    Raw SQL because ``CalendarEvent`` cannot express an explicit
    ``created_at`` (``save_event`` always stamps ``datetime('now')``).
    """
    await env.db.enqueue(
        """
        INSERT INTO calendar_events(
            id, calendar_id, summary, start_dt, end_dt, mirrored_from,
            origin, created_by, client_event_uuid, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            calendar_id,
            f"Summary {event_id}",
            "2025-06-01T10:00:00+00:00",
            "2025-06-01T11:00:00+00:00",
            mirrored_from,
            origin,
            "uid-alice",
            client_event_uuid,
            created_at,
            created_at,
        ),
    )


async def test_list_copies_groups_siblings_by_uuid(env):
    """Sibling rows across calendars come back grouped by uuid."""
    await _seed_user(env, "bob")
    await _seed_calendar(env, "cal-alice", "alice")
    await _seed_calendar(env, "cal-bob", "bob")
    await _insert_event(
        env, event_id="e1", calendar_id="cal-alice", client_event_uuid="u1"
    )
    await _insert_event(
        env, event_id="e2", calendar_id="cal-bob", client_event_uuid="u1"
    )
    await _insert_event(
        env, event_id="e3", calendar_id="cal-alice", client_event_uuid="u2"
    )

    out = await env.cal_repo.list_copies_for_client_event_uuids(["u1", "u2"])

    assert set(out) == {"u1", "u2"}
    assert [(c.event_id, c.calendar_id, c.owner_username) for c in out["u1"]] == [
        ("e1", "cal-alice", "alice"),
        ("e2", "cal-bob", "bob"),
    ]
    assert [c.event_id for c in out["u2"]] == ["e3"]


async def test_list_copies_excludes_remote_invite_rows(env):
    """Inbound peer rows share the peer's uuid but are not ours to touch."""
    await _seed_calendar(env, "cal-alice", "alice")
    await _insert_event(
        env, event_id="local-1", calendar_id="cal-alice", client_event_uuid="u1"
    )
    await _insert_event(
        env,
        event_id="remote-1",
        calendar_id="cal-alice",
        client_event_uuid="u1",
        origin="remote_invite",
    )

    out = await env.cal_repo.list_copies_for_client_event_uuids(["u1"])

    assert [c.event_id for c in out["u1"]] == ["local-1"]


async def test_list_copies_excludes_mirrored_rows(env):
    """A mirror of another event is not a fan-out sibling."""
    await _seed_user(env, "bob")
    await _seed_calendar(env, "cal-alice", "alice")
    await _seed_calendar(env, "cal-bob", "bob")
    await _insert_event(
        env, event_id="src", calendar_id="cal-alice", client_event_uuid="u1"
    )
    await _insert_event(
        env,
        event_id="mirror",
        calendar_id="cal-bob",
        client_event_uuid="u1",
        mirrored_from="src",
    )

    out = await env.cal_repo.list_copies_for_client_event_uuids(["u1"])

    assert [c.event_id for c in out["u1"]] == ["src"]


async def test_list_copies_excludes_space_calendars(env):
    """Rows on a space-type calendar are out of scope."""
    await _seed_calendar(env, "cal-alice", "alice")
    await _seed_calendar(env, "cal-space", "alice", calendar_type="space")
    await _insert_event(
        env, event_id="p1", calendar_id="cal-alice", client_event_uuid="u1"
    )
    await _insert_event(
        env, event_id="s1", calendar_id="cal-space", client_event_uuid="u1"
    )

    out = await env.cal_repo.list_copies_for_client_event_uuids(["u1"])

    assert [c.event_id for c in out["u1"]] == ["p1"]


async def test_list_copies_orders_copies_by_created_at_then_id(env):
    """Copies for one uuid come back in ``created_at, id`` order.

    ``ux_calendar_events_fanout`` (migration 0047) means there is at
    most one local copy per calendar, so the reachable shape is one row
    per calendar; the ordering makes the fan-out's result deterministic
    across them. ``id`` values are seeded in reverse alphabetical order
    of their ``created_at`` so an accidental ``ORDER BY id`` would fail
    this test.
    """
    await _seed_user(env, "bob")
    await _seed_user(env, "carol")
    await _seed_calendar(env, "cal-alice", "alice")
    await _seed_calendar(env, "cal-bob", "bob")
    await _seed_calendar(env, "cal-carol", "carol")
    await _insert_event(
        env,
        event_id="zzz-oldest",
        calendar_id="cal-alice",
        client_event_uuid="u1",
        created_at="2025-01-01 08:00:00",
    )
    await _insert_event(
        env,
        event_id="mmm-middle",
        calendar_id="cal-bob",
        client_event_uuid="u1",
        created_at="2025-01-01 09:00:00",
    )
    await _insert_event(
        env,
        event_id="aaa-newest",
        calendar_id="cal-carol",
        client_event_uuid="u1",
        created_at="2025-01-01 10:00:00",
    )

    out = await env.cal_repo.list_copies_for_client_event_uuids(["u1"])

    assert [c.event_id for c in out["u1"]] == [
        "zzz-oldest",
        "mmm-middle",
        "aaa-newest",
    ]
    assert [c.owner_username for c in out["u1"]] == ["alice", "bob", "carol"]


async def test_list_copies_unknown_uuid_absent(env):
    """An unknown uuid simply has no key in the result."""
    await _seed_calendar(env, "cal-alice", "alice")
    await _insert_event(
        env, event_id="e1", calendar_id="cal-alice", client_event_uuid="u1"
    )

    out = await env.cal_repo.list_copies_for_client_event_uuids(["u1", "nope"])

    assert "nope" not in out
    assert list(out) == ["u1"]


async def test_list_copies_empty_input_touches_no_db(env, monkeypatch):
    """Empty / blank input short-circuits before any query."""
    calls: list[str] = []
    original = env.db.fetchall

    async def counting(sql, params=()):
        calls.append(sql)
        return await original(sql, params)

    monkeypatch.setattr(env.db, "fetchall", counting)

    assert await env.cal_repo.list_copies_for_client_event_uuids([]) == {}
    assert await env.cal_repo.list_copies_for_client_event_uuids(["", "  "]) == {}
    assert calls == []


async def test_list_copies_chunks_large_uuid_lists(env, monkeypatch):
    """1200 uuids issue three chunked statements and merge results."""
    await _seed_calendar(env, "cal-alice", "alice")
    uuids = [f"u{i}" for i in range(1200)]
    await _insert_event(
        env, event_id="first", calendar_id="cal-alice", client_event_uuid="u0"
    )
    await _insert_event(
        env, event_id="last", calendar_id="cal-alice", client_event_uuid="u1199"
    )

    calls: list[str] = []
    original = env.db.fetchall

    async def counting(sql, params=()):
        calls.append(sql)
        return await original(sql, params)

    monkeypatch.setattr(env.db, "fetchall", counting)

    out = await env.cal_repo.list_copies_for_client_event_uuids(uuids)

    assert len(calls) == 3
    assert [c.event_id for c in out["u0"]] == ["first"]
    assert [c.event_id for c in out["u1199"]] == ["last"]


async def test_find_by_client_event_uuid_finds_the_local_copy(env):
    """The single local row on that calendar is returned.

    ``ux_calendar_events_fanout`` (migration 0047) guarantees at most
    one such row, so this is the whole reachable contract.
    """
    await _seed_user(env, "bob")
    await _seed_calendar(env, "cal-alice", "alice")
    await _seed_calendar(env, "cal-bob", "bob")
    await _insert_event(
        env, event_id="on-alice", calendar_id="cal-alice", client_event_uuid="u1"
    )
    await _insert_event(
        env, event_id="on-bob", calendar_id="cal-bob", client_event_uuid="u1"
    )

    found = await env.cal_repo.find_by_client_event_uuid("cal-alice", "u1")

    assert found is not None
    assert found.id == "on-alice"
    assert found.calendar_id == "cal-alice"


async def test_find_by_client_event_uuid_scoped_to_calendar(env):
    """A sibling on another calendar is not returned."""
    await _seed_user(env, "bob")
    await _seed_calendar(env, "cal-alice", "alice")
    await _seed_calendar(env, "cal-bob", "bob")
    await _insert_event(
        env, event_id="e1", calendar_id="cal-alice", client_event_uuid="u1"
    )

    assert await env.cal_repo.find_by_client_event_uuid("cal-bob", "u1") is None


async def test_find_by_client_event_uuid_ignores_remote_invite(env):
    """A remote_invite row never answers the lookup."""
    await _seed_calendar(env, "cal-alice", "alice")
    await _insert_event(
        env,
        event_id="remote-1",
        calendar_id="cal-alice",
        client_event_uuid="u1",
        origin="remote_invite",
    )

    assert await env.cal_repo.find_by_client_event_uuid("cal-alice", "u1") is None


async def test_find_by_client_event_uuid_ignores_mirrored_rows(env):
    """A space-calendar mirror is not a fan-out copy.

    ``SpaceRsvpMirrorBridge`` writes the personal mirror of a space
    event with ``origin='local'`` + ``mirrored_from=<source id>``, and
    ``CalendarService.update_event`` can PATCH a ``client_event_uuid``
    onto it. The reader, the ``ux_calendar_events_fanout`` index and
    migration 0047's de-dup scope all carry ``mirrored_from IS NULL``
    and must agree — otherwise the migration deletes a row no caller
    ever claimed.
    """
    await _seed_calendar(env, "cal-alice", "alice")
    await _insert_event(
        env,
        event_id="mirror-1",
        calendar_id="cal-alice",
        client_event_uuid="u1",
        mirrored_from="space-event-1",
    )

    assert await env.cal_repo.find_by_client_event_uuid("cal-alice", "u1") is None


async def test_list_copies_strips_whitespace_around_uuids(env):
    """A padded uuid must match — the filter strips, so the bind must too."""
    await _seed_calendar(env, "cal-alice", "alice")
    await _insert_event(
        env, event_id="e1", calendar_id="cal-alice", client_event_uuid="u1"
    )

    out = await env.cal_repo.list_copies_for_client_event_uuids([" u1 "])

    assert [c.event_id for c in out["u1"]] == ["e1"]


async def test_list_copies_dedupes_repeated_uuids(env, monkeypatch):
    """The same uuid twice binds once and yields one copy list."""
    await _seed_calendar(env, "cal-alice", "alice")
    await _insert_event(
        env, event_id="e1", calendar_id="cal-alice", client_event_uuid="u1"
    )

    bound: list[tuple] = []
    original = env.db.fetchall

    async def recording(sql, params=()):
        bound.append(tuple(params))
        return await original(sql, params)

    monkeypatch.setattr(env.db, "fetchall", recording)

    out = await env.cal_repo.list_copies_for_client_event_uuids(["u1", " u1 ", "u1"])

    assert bound == [("u1",)]
    assert [c.event_id for c in out["u1"]] == ["e1"]


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, []),
        (1, [1]),
        (499, [499]),
        (500, [500]),
        (501, [500, 1]),
        (1001, [500, 500, 1]),
    ],
)
def test_chunks_respects_the_host_parameter_ceiling(count, expected):
    """500 uuids is one statement; 501 is two — SQLite caps host params."""
    items = [f"u{i}" for i in range(count)]

    assert [len(c) for c in _chunks(items)] == expected

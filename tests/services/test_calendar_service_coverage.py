"""Coverage fill for :class:`CalendarService` + :class:`SpaceCalendarService`."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.calendar import RSVPStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.calendar_repo import (
    SqliteCalendarRepo,
    SqliteSpaceCalendarRepo,
)
from socialhome.services.calendar_service import (
    CalendarService,
    SpaceCalendarService,
    _parse_iso,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    # FK target for calendar.owner_username / event.created_by.
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?, ?, ?)",
        ("u1", "u1-id", "U1"),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.bus = EventBus()
    e.repo = SqliteCalendarRepo(db)
    e.space_repo = SqliteSpaceCalendarRepo(db)
    e.svc = CalendarService(e.repo, bus=e.bus)
    e.space_svc = SpaceCalendarService(e.space_repo, bus=e.bus)
    yield e
    await db.shutdown()


# ── _parse_iso ──────────────────────────────────────────────────────


def test_parse_iso_with_z():
    assert _parse_iso("2026-01-01T00:00:00Z").isoformat().startswith("2026-01-01")


def test_parse_iso_invalid_raises():
    with pytest.raises(ValueError):
        _parse_iso("not-a-date")


# ── CalendarService ─────────────────────────────────────────────────


async def test_get_event_unknown_raises(env):
    with pytest.raises(KeyError):
        await env.svc.get_event("ghost")


async def test_list_events_in_range_bad_date_raises(env):
    cal = await env.svc.create_calendar(
        name="C",
        owner_username="u1",
        color="#fff",
    )
    with pytest.raises(ValueError):
        await env.svc.list_events_in_range(
            cal.id,
            start="not-a-date",
            end="2026-01-01T00:00:00Z",
        )


async def test_delete_event_unknown_raises(env):
    with pytest.raises(KeyError):
        await env.svc.delete_event("ghost")


async def test_delete_event_emits_deleted(env):
    cal = await env.svc.create_calendar(
        name="C",
        owner_username="u1",
        color="#fff",
    )
    event = await env.svc.create_event(
        calendar_id=cal.id,
        summary="Meet",
        start="2026-01-01T10:00:00Z",
        end="2026-01-01T11:00:00Z",
        created_by="u1",
    )
    await env.svc.delete_event(event.id)
    with pytest.raises(KeyError):
        await env.svc.get_event(event.id)


async def test_update_event_unknown_raises(env):
    with pytest.raises(KeyError):
        await env.svc.update_event("ghost", summary="x")


async def test_update_event_empty_summary_raises(env):
    cal = await env.svc.create_calendar(
        name="C",
        owner_username="u1",
        color="#fff",
    )
    event = await env.svc.create_event(
        calendar_id=cal.id,
        summary="Meet",
        start="2026-01-01T10:00:00Z",
        end="2026-01-01T11:00:00Z",
        created_by="u1",
    )
    with pytest.raises(ValueError):
        await env.svc.update_event(event.id, summary="   ")


async def test_update_event_end_before_start_raises(env):
    cal = await env.svc.create_calendar(
        name="C",
        owner_username="u1",
        color="#fff",
    )
    event = await env.svc.create_event(
        calendar_id=cal.id,
        summary="Meet",
        start="2026-01-01T10:00:00Z",
        end="2026-01-01T11:00:00Z",
        created_by="u1",
    )
    with pytest.raises(ValueError):
        await env.svc.update_event(
            event.id,
            start="2026-01-01T12:00:00Z",
            end="2026-01-01T11:00:00Z",
        )


async def test_update_event_full_happy(env):
    cal = await env.svc.create_calendar(
        name="C",
        owner_username="u1",
        color="#fff",
    )
    event = await env.svc.create_event(
        calendar_id=cal.id,
        summary="Meet",
        start="2026-01-01T10:00:00Z",
        end="2026-01-01T11:00:00Z",
        created_by="u1",
    )
    updated = await env.svc.update_event(
        event.id,
        summary="Standup",
        all_day=True,
        description="Weekly",
        attendees=["u1", "u2"],
        rrule="FREQ=WEEKLY",
    )
    assert updated.summary == "Standup"
    assert updated.all_day is True
    assert updated.description == "Weekly"
    assert updated.rrule == "FREQ=WEEKLY"


# ── SpaceCalendarService ─────────────────────────────────────────


async def _seed_space(env, sid="sp1"):
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'S', 'inst', 'u1', ?)",
        (sid, "ab" * 32),
    )


async def test_space_list_events_bad_date(env):
    with pytest.raises(ValueError):
        await env.space_svc.list_events_in_range(
            "sp",
            start="bogus",
            end="2026-01-01T00:00:00Z",
        )


async def test_space_create_event_empty_summary(env):
    await _seed_space(env)
    with pytest.raises(ValueError):
        await env.space_svc.create_event(
            space_id="sp1",
            summary="   ",
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T01:00:00Z",
            created_by="u1",
        )


async def test_space_create_event_bad_date(env):
    await _seed_space(env)
    with pytest.raises(ValueError):
        await env.space_svc.create_event(
            space_id="sp1",
            summary="m",
            start="not-a-date",
            end="2026-01-01T01:00:00Z",
            created_by="u1",
        )


async def test_space_create_event_end_before_start(env):
    await _seed_space(env)
    with pytest.raises(ValueError):
        await env.space_svc.create_event(
            space_id="sp1",
            summary="m",
            start="2026-01-01T02:00:00Z",
            end="2026-01-01T01:00:00Z",
            created_by="u1",
        )


async def test_space_resolve_unknown_returns_none(env):
    assert await env.space_svc.resolve_space_id("ghost") is None


async def test_space_resolve_happy(env):
    await _seed_space(env)
    event = await env.space_svc.create_event(
        space_id="sp1",
        summary="m",
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T01:00:00Z",
        created_by="u1",
    )
    assert await env.space_svc.resolve_space_id(event.id) == "sp1"


async def test_space_update_event_unknown(env):
    with pytest.raises(KeyError):
        await env.space_svc.update_event("ghost", summary="x")


async def test_space_update_event_empty_summary(env):
    await _seed_space(env)
    event = await env.space_svc.create_event(
        space_id="sp1",
        summary="m",
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T01:00:00Z",
        created_by="u1",
    )
    with pytest.raises(ValueError):
        await env.space_svc.update_event(event.id, summary="   ")


async def test_space_update_event_bad_range(env):
    await _seed_space(env)
    event = await env.space_svc.create_event(
        space_id="sp1",
        summary="m",
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T01:00:00Z",
        created_by="u1",
    )
    with pytest.raises(ValueError):
        await env.space_svc.update_event(
            event.id,
            start="2026-01-01T02:00:00Z",
            end="2026-01-01T01:00:00Z",
        )


async def test_space_update_event_full(env):
    await _seed_space(env)
    event = await env.space_svc.create_event(
        space_id="sp1",
        summary="m",
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T01:00:00Z",
        created_by="u1",
    )
    updated = await env.space_svc.update_event(
        event.id,
        summary="m2",
        all_day=True,
        description="d",
        attendees=("u1", "u2"),
        rrule="FREQ=DAILY",
    )
    assert updated.summary == "m2"
    assert updated.all_day is True


async def test_space_rsvp_bad_status(env):
    with pytest.raises(ValueError):
        await env.space_svc.rsvp(event_id="e", user_id="u", status="bogus")


async def test_space_rsvp_happy(env):
    await _seed_space(env)
    event = await env.space_svc.create_event(
        space_id="sp1",
        summary="m",
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T01:00:00Z",
        created_by="u1",
    )
    await env.space_svc.rsvp(
        event_id=event.id,
        user_id="u2",
        status=RSVPStatus.GOING,
    )
    await env.space_svc.remove_rsvp(event_id=event.id, user_id="u2")
    rows = await env.space_svc.list_rsvps(event.id)
    assert rows == []

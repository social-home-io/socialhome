"""Tests for :class:`SpaceRsvpMirrorBridge`.

Going RSVPs on space events appear on the user's personal calendar
as mirror rows; switching to maybe / declined / waitlist drops the
mirror; source-event edits flow through; source-event deletion drops
all mirrors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    SpaceMemberLeft,
    SpaceRsvpChanged,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.calendar_repo import (
    SqliteCalendarRepo,
    SqliteSpaceCalendarRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.calendar_service import (
    CalendarService,
    SpaceCalendarService,
)
from socialhome.services.space_rsvp_mirror_bridge import (
    SpaceRsvpMirrorBridge,
    _mint_mirror_id,
)


@pytest.fixture
async def env(tmp_dir):
    """A wired bridge over real repos + a real bus + an in-memory DB."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    # Local user with their own personal calendar.
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    # Space the events live in.
    await db.enqueue(
        "INSERT INTO spaces(id, name, space_type, owner_instance_id,"
        " owner_username, identity_public_key) VALUES(?,?,?,?,?,?)",
        ("space-1", "Family", "household", iid, "anna", "ab" * 32),
    )

    bus = EventBus()
    cal_repo = SqliteCalendarRepo(db)
    space_cal_repo = SqliteSpaceCalendarRepo(db)
    user_repo = SqliteUserRepo(db)
    cal_svc = CalendarService(cal_repo, bus)
    space_cal_svc = SpaceCalendarService(space_cal_repo, bus)
    space_cal_svc.wire()  # SpaceMemberLeft → drops RSVPs
    bridge = SpaceRsvpMirrorBridge(
        bus=bus,
        calendar_repo=cal_repo,
        space_calendar_repo=space_cal_repo,
        user_repo=user_repo,
    )
    bridge.wire()

    # The mirror needs a personal calendar to land on — create one.
    personal_cal = await cal_svc.create_calendar(
        name="Anna",
        owner_username="anna",
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.bus = bus
    e.cal_repo = cal_repo
    e.space_cal_repo = space_cal_repo
    e.user_repo = user_repo
    e.cal_svc = cal_svc
    e.space_cal_svc = space_cal_svc
    e.bridge = bridge
    e.personal_cal = personal_cal
    yield e
    await db.shutdown()


async def _create_space_event(
    space_cal_svc: SpaceCalendarService,
    *,
    summary: str = "Movie night",
    user_id: str = "u-anna",
) -> str:
    """Create a space event WITHOUT going through the auto-RSVP path's
    bridge subscriber. Returns the new event id.

    The auto-RSVP at create time fires SpaceRsvpChanged for the
    creator — we want a clean baseline for most tests, so we create
    the event with a different ``created_by`` and let the test trigger
    the going-RSVP explicitly.
    """
    now = datetime.now(timezone.utc)
    ev = await space_cal_svc.create_event(
        space_id="space-1",
        summary=summary,
        start=now.isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
        created_by=user_id,
    )
    return ev.id


async def test_going_rsvp_creates_mirror_on_personal_calendar(env):
    """The user creates a space event (auto-RSVP'd as going) → a
    mirror appears on their personal calendar via the bridge."""
    event_id = await _create_space_event(env.space_cal_svc)
    mirror_id = _mint_mirror_id("u-anna", event_id)
    mirror = await env.cal_repo.get_event(mirror_id)
    assert mirror is not None
    assert mirror.summary == "Movie night"
    assert mirror.calendar_id == env.personal_cal.id
    assert mirror.mirrored_from == event_id
    assert mirror.created_by == "u-anna"
    assert mirror.origin == "local"


async def test_declined_rsvp_removes_mirror(env):
    event_id = await _create_space_event(env.space_cal_svc)
    # Confirm baseline mirror exists from auto-RSVP.
    mirror_id = _mint_mirror_id("u-anna", event_id)
    assert await env.cal_repo.get_event(mirror_id) is not None
    # Switch to declined → bridge drops mirror.
    await env.space_cal_svc.rsvp(
        event_id=event_id,
        user_id="u-anna",
        status="declined",
    )
    assert await env.cal_repo.get_event(mirror_id) is None


async def test_remove_rsvp_removes_mirror(env):
    event_id = await _create_space_event(env.space_cal_svc)
    mirror_id = _mint_mirror_id("u-anna", event_id)
    assert await env.cal_repo.get_event(mirror_id) is not None
    await env.space_cal_svc.remove_rsvp(
        event_id=event_id,
        user_id="u-anna",
    )
    assert await env.cal_repo.get_event(mirror_id) is None


async def test_source_update_refreshes_mirror(env):
    event_id = await _create_space_event(env.space_cal_svc)
    mirror_id = _mint_mirror_id("u-anna", event_id)
    new_start = datetime.now(timezone.utc) + timedelta(days=1)
    new_end = new_start + timedelta(hours=3)
    await env.space_cal_svc.update_event(
        event_id,
        space_id="space-1",
        summary="Movie night (rescheduled)",
        start=new_start.isoformat(),
        end=new_end.isoformat(),
    )
    mirror = await env.cal_repo.get_event(mirror_id)
    assert mirror is not None
    assert mirror.summary == "Movie night (rescheduled)"
    # Compare date-time fields by ISO since the source preserves tz.
    assert mirror.start.isoformat() == new_start.isoformat()
    assert mirror.end.isoformat() == new_end.isoformat()


async def test_source_delete_drops_mirror(env):
    event_id = await _create_space_event(env.space_cal_svc)
    mirror_id = _mint_mirror_id("u-anna", event_id)
    assert await env.cal_repo.get_event(mirror_id) is not None
    await env.space_cal_svc.delete_event(event_id, space_id="space-1")
    assert await env.cal_repo.get_event(mirror_id) is None


async def test_member_left_drops_mirror(env):
    """When a member leaves a space, _on_member_left removes their
    RSVPs — each removal fires SpaceRsvpChanged(status=None) and the
    bridge drops the mirror."""
    event_id = await _create_space_event(env.space_cal_svc)
    mirror_id = _mint_mirror_id("u-anna", event_id)
    assert await env.cal_repo.get_event(mirror_id) is not None
    await env.bus.publish(
        SpaceMemberLeft(space_id="space-1", user_id="u-anna"),
    )
    assert await env.cal_repo.get_event(mirror_id) is None


async def test_idempotent_under_redelivered_rsvp(env):
    """Same SpaceRsvpChanged dispatched twice collapses onto the same
    row, never creates a duplicate."""
    event_id = await _create_space_event(env.space_cal_svc)
    await env.bus.publish(
        SpaceRsvpChanged(
            event_id=event_id,
            space_id="space-1",
            user_id="u-anna",
            occurrence_at=datetime.now(timezone.utc).isoformat(),
            status="going",
        )
    )
    mirrors = await env.cal_repo.list_mirrors_of(event_id)
    assert len(mirrors) == 1


async def test_remote_user_rsvp_does_not_create_local_mirror(env):
    """A federated RSVP from a remote user should NOT land on any
    local calendar — the remote user's mirror lives on their own
    instance. Verifies the local-user gate."""
    event_id = await _create_space_event(env.space_cal_svc)
    await env.bus.publish(
        SpaceRsvpChanged(
            event_id=event_id,
            space_id="space-1",
            user_id="u-bob-remote",
            occurrence_at=datetime.now(timezone.utc).isoformat(),
            status="going",
        )
    )
    mirrors = await env.cal_repo.list_mirrors_of(event_id)
    # Only the local creator's mirror exists; no row for u-bob-remote.
    assert len(mirrors) == 1
    assert mirrors[0].created_by == "u-anna"


async def test_user_with_no_personal_calendar_is_skipped(env):
    """If the user has no personal calendar, the bridge logs and
    skips — never raises, never silently writes to a calendar that
    doesn't belong to them."""
    # Seed a second user without any personal calendar.
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("ben", "u-ben", "Ben"),
    )
    event_id = await _create_space_event(env.space_cal_svc)
    await env.space_cal_svc.rsvp(
        event_id=event_id,
        user_id="u-ben",
        status="going",
    )
    mirror_id = _mint_mirror_id("u-ben", event_id)
    assert await env.cal_repo.get_event(mirror_id) is None

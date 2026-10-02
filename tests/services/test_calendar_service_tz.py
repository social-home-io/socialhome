"""Tz resolution chain + DST-correct recurrence for calendar events.

These tests exercise the boundary the SPA stamps at create time
(``event.tz``), the service-layer fallback when the SPA omits one, and
the load-bearing invariant that a recurring event in a non-UTC zone
preserves its wall-clock anchor across DST transitions.
"""

from __future__ import annotations

import pytest

from socialhome.domain.space import SpaceFeatures
from socialhome.repositories.space_repo import SqliteSpaceRepo

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.calendar import Calendar
from socialhome.repositories.calendar_repo import (
    SqliteCalendarRepo,
    SqliteSpaceCalendarRepo,
)
from socialhome.repositories.preferences_repo import SqlitePreferencesRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.calendar_service import (
    CalendarService,
    SpaceCalendarService,
)
from socialhome.services.preferences_service import PreferencesService


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "tz.db", batch_timeout_ms=10)
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
    e.cal_repo = SqliteCalendarRepo(db)
    e.space_cal_repo = SqliteSpaceCalendarRepo(db)
    e.user_repo = SqliteUserRepo(db)
    e.household_repo = SqlitePreferencesRepo(db)
    e.household_svc = PreferencesService(repo=e.household_repo)
    e.cal_svc = CalendarService(e.cal_repo)
    e.space_cal_svc = SpaceCalendarService(e.space_cal_repo)
    e.space_cal_svc.attach_space_repo(SqliteSpaceRepo(db))

    # Plumb the same dependencies wire_extras would set up in app.py.
    class _UserRepoFacade:
        """Minimal facade exposing the calendar service's expected
        ``get(username) -> User``."""

        def __init__(self, real):
            self._real = real

        async def get(self, username):
            return await self._real.get(username)

        async def get_instance_for_user(self, *args, **kwargs):
            return None

    e.cal_svc._user_repo = _UserRepoFacade(e.user_repo)
    e.cal_svc.attach_household_features(e.household_svc)
    e.space_cal_svc.attach_household_features(e.household_svc)
    yield e
    await db.shutdown()


async def _make_user(env, username: str, tz: str = "UTC") -> None:
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name, tz) VALUES(?,?,?,?)",
        (username, f"uid-{username}", username.title(), tz),
    )


async def _make_calendar(env, *, owner: str) -> Calendar:
    return await env.cal_repo.save_calendar(
        Calendar(
            id=f"cal-{owner}",
            name=f"{owner}'s calendar",
            owner_username=owner,
            color="#4A90E2",
        ),
    )


# ── Personal calendar: tz resolution chain ──────────────────────────────


async def test_personal_event_takes_explicit_tz(env):
    """A POST that carries ``tz`` wins over every fallback layer."""
    await _make_user(env, "anna", tz="Europe/Berlin")
    await env.household_svc.set_tz_from_ha("Europe/Berlin")
    await _make_calendar(env, owner="anna")
    event = await env.cal_svc.create_event(
        calendar_id="cal-anna",
        summary="Dentist",
        start="2026-04-01T10:00:00Z",
        end="2026-04-01T11:00:00Z",
        created_by="uid-anna",
        tz="America/New_York",
    )
    assert event.tz == "America/New_York"


async def test_personal_event_falls_back_to_user_tz(env):
    """No explicit tz → falls back to the calendar owner's ``users.tz``."""
    await _make_user(env, "anna", tz="Europe/Berlin")
    await env.household_svc.set_tz_from_ha("America/New_York")
    await _make_calendar(env, owner="anna")
    event = await env.cal_svc.create_event(
        calendar_id="cal-anna",
        summary="Dentist",
        start="2026-04-01T10:00:00Z",
        end="2026-04-01T11:00:00Z",
        created_by="uid-anna",
    )
    assert event.tz == "Europe/Berlin"


async def test_personal_event_falls_back_to_household_when_user_tz_is_utc(env):
    """User still at the install-default UTC → household tz wins."""
    await _make_user(env, "anna", tz="UTC")
    await env.household_svc.set_tz_from_ha("Europe/Berlin")
    await _make_calendar(env, owner="anna")
    event = await env.cal_svc.create_event(
        calendar_id="cal-anna",
        summary="Dentist",
        start="2026-04-01T10:00:00Z",
        end="2026-04-01T11:00:00Z",
        created_by="uid-anna",
    )
    assert event.tz == "Europe/Berlin"


async def test_personal_event_unknown_tz_is_dropped(env):
    """A malformed IANA name from a hand-rolled client falls back to
    the user / household layer rather than 400'ing the create."""
    await _make_user(env, "anna", tz="Europe/Berlin")
    await env.household_svc.set_tz_from_ha("Europe/Berlin")
    await _make_calendar(env, owner="anna")
    event = await env.cal_svc.create_event(
        calendar_id="cal-anna",
        summary="Dentist",
        start="2026-04-01T10:00:00Z",
        end="2026-04-01T11:00:00Z",
        created_by="uid-anna",
        tz="Mars/Olympus_Mons",
    )
    assert event.tz == "Europe/Berlin"


# ── Round-trip: tz survives save → reload ───────────────────────────────


async def test_event_tz_roundtrips_through_repo(env):
    """The tz column survives a save → reload cycle (event lookup)."""
    await _make_user(env, "anna", tz="Europe/Berlin")
    await _make_calendar(env, owner="anna")
    event = await env.cal_svc.create_event(
        calendar_id="cal-anna",
        summary="Dentist",
        start="2026-04-01T10:00:00Z",
        end="2026-04-01T11:00:00Z",
        created_by="uid-anna",
        tz="America/Los_Angeles",
    )
    reloaded = await env.cal_repo.get_event(event.id)
    assert reloaded is not None
    assert reloaded.tz == "America/Los_Angeles"


# ── Space calendar: tz inherited from space row ─────────────────────────


async def test_space_event_inherits_space_tz(env, monkeypatch):
    """Space events inherit ``space.tz`` when no explicit tz is given,
    independent of any user's preference."""

    # Stub the space repo lookup the service uses for the fallback chain.
    class _StubSpaceRepo:
        async def get(self, space_id: str):
            class _S:
                id = "space-1"
                tz = "Asia/Tokyo"
                features = SpaceFeatures()  # OPEN — no access gate

            return _S() if space_id == "space-1" else None

    env.space_cal_svc.attach_space_repo(_StubSpaceRepo())
    event = await env.space_cal_svc.create_event(
        space_id="space-1",
        summary="Standup",
        start="2026-04-01T01:00:00Z",
        end="2026-04-01T02:00:00Z",
        created_by="uid-anna",
    )
    assert event.tz == "Asia/Tokyo"


# ── DST: recurring events preserve wall clock across the spring shift ──


async def test_recurring_event_preserves_wall_clock_across_dst(env):
    """A weekly "Tue 19:00 Europe/Berlin" recurring event stays at
    19:00 Berlin (= 18:00Z in winter, 17:00Z after the March DST shift)
    across the spring-forward boundary. This is the load-bearing
    invariant for §17.2 RRULE expansion under the new tz column.
    """
    await _make_user(env, "anna", tz="Europe/Berlin")
    await _make_calendar(env, owner="anna")
    # Mar 24 2026 (winter) 19:00 Berlin = 18:00 UTC.
    event = await env.cal_svc.create_event(
        calendar_id="cal-anna",
        summary="Weekly check-in",
        start="2026-03-24T18:00:00Z",
        end="2026-03-24T19:00:00Z",
        created_by="uid-anna",
        rrule="FREQ=WEEKLY;COUNT=3",
        tz="Europe/Berlin",
    )
    from datetime import datetime, timezone

    occurrences = await env.cal_repo.list_events_in_range(
        event.calendar_id,
        start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    starts = [e.start.isoformat() for e in occurrences]
    assert starts == [
        "2026-03-24T18:00:00+00:00",  # Mar 24 winter — 19:00 Berlin
        "2026-03-31T17:00:00+00:00",  # Mar 31 after DST — still 19:00 Berlin
        "2026-04-07T17:00:00+00:00",  # Apr 7 summer — still 19:00 Berlin
    ]

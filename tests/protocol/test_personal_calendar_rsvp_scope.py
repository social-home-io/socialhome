"""Release-blocker protocol tests: a personal-calendar RSVP answers one invite.

Marked ``@pytest.mark.security``.

``PERSONAL_CALENDAR_RSVP_UPDATED`` / ``_DELETED`` carry a bare local
``event_id`` and a ``user_id``. The rule these tests encode, against the
real application registry and SQLite:

    A household may set or clear an RSVP only on an event this household
    organised and invited it to, and only for its own user who is on that
    event's attendee list. Never for a local member, never for another
    household's invitee, never on an event it wasn't invited to.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.calendar import Calendar, CalendarEvent, CalendarRSVP
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.repositories.calendar_repo import SqliteCalendarRepo

pytestmark = pytest.mark.security

FET = FederationEventType

INVITED = "peer-invited"  # household the event was shared with
OTHER = "peer-other"  # a paired household the event was never shared with
START = datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc)
OCC = START.isoformat()


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "rsvp.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, user_id: str) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            "00" * 32,
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, user_id, user_id),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "u-anna", "Anna"),
    )
    await _seed_peer(db, INVITED, "u-bob")
    await _seed_peer(db, OTHER, "u-dora")
    repo = SqliteCalendarRepo(db)
    await repo.save_calendar(
        Calendar(id="cal-anna", name="Anna", color="#abcdef", owner_username="anna")
    )
    # Organised here; invites Bob (on INVITED) and Dora (on OTHER).
    await repo.save_event(
        CalendarEvent(
            id="ev-picnic",
            calendar_id="cal-anna",
            summary="Picnic",
            start=START,
            end=START + timedelta(hours=2),
            created_by="u-anna",
            attendees=("u-bob", "u-dora"),
            rsvp_enabled=True,
        )
    )
    # Organised here; shared with nobody.
    await repo.save_event(
        CalendarEvent(
            id="ev-private",
            calendar_id="cal-anna",
            summary="Dentist",
            start=START,
            end=START + timedelta(hours=1),
            created_by="u-anna",
        )
    )
    for event_id, user_id in (
        ("ev-picnic", "u-anna"),
        ("ev-picnic", "u-dora"),
        ("ev-private", "u-anna"),
    ):
        await repo.upsert_rsvp(
            CalendarRSVP(
                event_id=event_id,
                user_id=user_id,
                status="accepted",
                updated_at="2026-05-01T00:00:00+00:00",
                occurrence_at=OCC,
            )
        )
    return app, db


async def _rsvps(db) -> list[tuple]:
    rows = await db.fetchall(
        "SELECT event_id, user_id, status, occurrence_at FROM calendar_event_rsvps"
        " ORDER BY event_id, user_id",
        (),
    )
    return [tuple(r) for r in rows]


async def _send(app, event_type, payload, *, from_instance=INVITED) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


_ATTACKS = [
    pytest.param("ev-picnic", "u-anna", INVITED, id="a local member's RSVP"),
    pytest.param("ev-picnic", "u-dora", INVITED, id="another household's invitee"),
    pytest.param("ev-private", "u-bob", INVITED, id="an event never shared"),
    pytest.param("ev-private", "u-anna", INVITED, id="local RSVP on unshared event"),
    pytest.param("ev-picnic", "u-bob", OTHER, id="invitee spoofed by another peer"),
]


@pytest.mark.parametrize(("event_id", "user_id", "sender"), _ATTACKS)
async def test_rsvp_update_outside_the_invite_changes_nothing(
    env, event_id, user_id, sender
):
    app, db = env
    before = await _rsvps(db)
    await _send(
        app,
        FET.PERSONAL_CALENDAR_RSVP_UPDATED,
        {
            "event_id": event_id,
            "user_id": user_id,
            "status": "declined",
            "occurrence_at": OCC,
        },
        from_instance=sender,
    )
    assert await _rsvps(db) == before


@pytest.mark.parametrize(("event_id", "user_id", "sender"), _ATTACKS)
async def test_rsvp_delete_outside_the_invite_changes_nothing(
    env, event_id, user_id, sender
):
    app, db = env
    before = await _rsvps(db)
    await _send(
        app,
        FET.PERSONAL_CALENDAR_RSVP_DELETED,
        {"event_id": event_id, "user_id": user_id, "occurrence_at": OCC},
        from_instance=sender,
    )
    assert await _rsvps(db) == before


async def test_the_invited_household_answers_for_its_own_invitee(env):
    """Control: the legitimate reply lands and can be withdrawn."""
    app, db = env
    await _send(
        app,
        FET.PERSONAL_CALENDAR_RSVP_UPDATED,
        {"event_id": "ev-picnic", "user_id": "u-bob", "status": "tentative"},
    )
    assert ("ev-picnic", "u-bob", "tentative", OCC) in await _rsvps(db)
    await _send(
        app,
        FET.PERSONAL_CALENDAR_RSVP_DELETED,
        {"event_id": "ev-picnic", "user_id": "u-bob", "occurrence_at": OCC},
    )
    assert all(r[1] != "u-bob" for r in await _rsvps(db))

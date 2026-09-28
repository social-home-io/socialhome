"""Release-blocker protocol tests: a personal-calendar invite touches only its own mirror.

Marked ``@pytest.mark.security``.

``PERSONAL_CALENDAR_EVENT_CREATED`` / ``_UPDATED`` / ``_DELETED`` carry a
peer-chosen remote ``event_id``, an attendee list and an organiser id.
The receiver mirrors the invite into each local attendee's personal
calendar under a row id derived from (sending household, remote event,
recipient). The rule these tests encode, against the real application
registry and SQLite:

    A household may create, update or delete only its own mirror of its
    own event for a local recipient. A row found at the derived id that is
    not exactly that mirror (a local event, another household's mirror, a
    mirror of a different remote event, a mirror on someone else's
    calendar) is left untouched. The organiser must be a user of the
    sending household. Remote attendees never get a row here.
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
from socialhome.services.federation_inbound.personal_calendar import (
    _mint_event_id,
)

pytestmark = pytest.mark.security

FET = FederationEventType

# Two paired households whose ids share the prefix the derived row id keeps,
# so their derived ids for the same remote event id coincide.
PEER = "household-ab-peer"
OTHER = "household-ab-other"
ANNA = "u-anna-0000000000000000000000001"
CARL = "u-carl-0000000000000000000000001"
# Shares the derived-id prefix with ANNA but is not a local user.
ANNA_LOOKALIKE = "u-anna-0000000000000000000000002"
BOB = "u-bob-peer"
DORA = "u-dora-other"

PEER_REMOTE = "peer-event-0000000000000000000001"
# Shares the first 24 characters with PEER_REMOTE.
PEER_REMOTE_TWIN = "peer-event-0000000000000000000002"
OTHER_REMOTE = "other-event-01"
CRAFTED_REMOTE = "crafted-01"

START = datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc)
OCC = START.isoformat()


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "pcal.db"),
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


def _mirror(row_id: str, *, instance: str, remote_id: str, organiser: str):
    return CalendarEvent(
        id=row_id,
        calendar_id="cal-anna",
        summary=f"Invite from {instance}",
        start=START,
        end=START + timedelta(hours=2),
        created_by=organiser,
        attendees=(ANNA,),
        rsvp_enabled=True,
        origin="remote_invite",
        remote_event_id=remote_id,
        remote_instance_id=instance,
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for username, user_id in (("anna", ANNA), ("carl", CARL)):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, user_id, username.title()),
        )
    await _seed_peer(db, PEER, BOB)
    await _seed_peer(db, OTHER, DORA)
    repo = SqliteCalendarRepo(db)
    for username in ("anna", "carl"):
        await repo.save_calendar(
            Calendar(
                id=f"cal-{username}",
                name=username.title(),
                color="#abcdef",
                owner_username=username,
            )
        )
    # Anna's own event, sitting at the id a crafted invite would derive.
    await repo.save_event(
        CalendarEvent(
            id=_mint_event_id(PEER, CRAFTED_REMOTE, ANNA),
            calendar_id="cal-anna",
            summary="Dentist",
            start=START,
            end=START + timedelta(hours=1),
            created_by=ANNA,
        )
    )
    # PEER's invite, mirrored for Anna.
    await repo.save_event(
        _mirror(
            _mint_event_id(PEER, PEER_REMOTE, ANNA),
            instance=PEER,
            remote_id=PEER_REMOTE,
            organiser=BOB,
        )
    )
    # OTHER's invite, mirrored for Anna.
    await repo.save_event(
        _mirror(
            _mint_event_id(OTHER, OTHER_REMOTE, ANNA),
            instance=OTHER,
            remote_id=OTHER_REMOTE,
            organiser=DORA,
        )
    )
    for row_id in (
        _mint_event_id(PEER, PEER_REMOTE, ANNA),
        _mint_event_id(OTHER, OTHER_REMOTE, ANNA),
    ):
        await repo.upsert_rsvp(
            CalendarRSVP(
                event_id=row_id,
                user_id=ANNA,
                status="accepted",
                updated_at="2026-05-01T00:00:00+00:00",
                occurrence_at=OCC,
            )
        )
    return app, db


async def _snapshot(db) -> tuple[list[dict], list[dict]]:
    events = await db.fetchall("SELECT * FROM calendar_events ORDER BY id", ())
    rsvps = await db.fetchall(
        "SELECT * FROM calendar_event_rsvps ORDER BY event_id, user_id", ()
    )
    return [dict(r) for r in events], [dict(r) for r in rsvps]


def _row(events: list[dict], row_id: str) -> dict | None:
    return next((r for r in events if r["id"] == row_id), None)


async def _send(app, event_type, payload, *, from_instance=PEER) -> None:
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


def _invite(remote_id: str, *, organiser: str = BOB, attendees=(ANNA,)) -> dict:
    return {
        "event_id": remote_id,
        "summary": "Hijacked",
        "start": (START + timedelta(days=1)).isoformat(),
        "end": (START + timedelta(days=1, hours=1)).isoformat(),
        "organizer_user_id": organiser,
        "attendee_user_ids": list(attendees),
        "rsvp_enabled": True,
    }


_SAVE_ATTACKS = [
    pytest.param(_invite(OTHER_REMOTE), id="collides with another household's mirror"),
    pytest.param(_invite(PEER_REMOTE_TWIN), id="collides with a different own event"),
    pytest.param(_invite(CRAFTED_REMOTE), id="collides with a local event"),
    pytest.param(
        _invite(PEER_REMOTE, attendees=(ANNA_LOOKALIKE,)),
        id="recipient is a look-alike of a local user",
    ),
    pytest.param(_invite("fresh-01", organiser=ANNA), id="organiser spoofed as local"),
    pytest.param(
        _invite("fresh-01", organiser=DORA), id="organiser spoofed as third household"
    ),
    pytest.param(_invite("fresh-01", organiser="u-ghost"), id="organiser unknown"),
    pytest.param(_invite("fresh-01", attendees=(DORA,)), id="remote attendee"),
    pytest.param(_invite("fresh-01", attendees=(BOB,)), id="sender's own attendee"),
]


@pytest.mark.parametrize(
    "event_type",
    [FET.PERSONAL_CALENDAR_EVENT_CREATED, FET.PERSONAL_CALENDAR_EVENT_UPDATED],
)
@pytest.mark.parametrize("payload", _SAVE_ATTACKS)
async def test_invite_save_outside_its_own_mirror_changes_nothing(
    env, event_type, payload
):
    app, db = env
    before = await _snapshot(db)
    await _send(app, event_type, payload)
    assert await _snapshot(db) == before


_DELETE_ATTACKS = [
    pytest.param(
        {"event_id": OTHER_REMOTE, "attendee_user_ids": [ANNA]},
        PEER,
        id="collides with another household's mirror",
    ),
    pytest.param(
        {"event_id": OTHER_REMOTE},
        PEER,
        id="another household's event without attendee list",
    ),
    pytest.param(
        {"event_id": PEER_REMOTE_TWIN, "attendee_user_ids": [ANNA]},
        PEER,
        id="collides with a different own event",
    ),
    pytest.param(
        {"event_id": CRAFTED_REMOTE, "attendee_user_ids": [ANNA]},
        PEER,
        id="collides with a local event",
    ),
    pytest.param(
        {"event_id": PEER_REMOTE, "attendee_user_ids": [ANNA]},
        OTHER,
        id="another household deletes PEER's invite",
    ),
]


@pytest.mark.parametrize(("payload", "sender"), _DELETE_ATTACKS)
async def test_invite_delete_outside_its_own_mirror_changes_nothing(
    env, payload, sender
):
    app, db = env
    before = await _snapshot(db)
    await _send(app, FET.PERSONAL_CALENDAR_EVENT_DELETED, payload, from_instance=sender)
    assert await _snapshot(db) == before


async def test_the_sending_household_manages_its_own_invite(env):
    """Control: create -> update -> delete of PEER's own invite for Anna."""
    app, db = env
    row_id = _mint_event_id(PEER, "fresh-01", ANNA)
    await _send(app, FET.PERSONAL_CALENDAR_EVENT_CREATED, _invite("fresh-01"))
    events, rsvps = await _snapshot(db)
    row = _row(events, row_id)
    assert row is not None
    assert row["calendar_id"] == "cal-anna"
    assert row["created_by"] == BOB
    assert row["origin"] == "remote_invite"
    assert row["remote_event_id"] == "fresh-01"
    assert row["remote_instance_id"] == PEER
    assert [(r["user_id"], r["status"]) for r in rsvps if r["event_id"] == row_id] == [
        (ANNA, "tentative")
    ]

    await _send(
        app,
        FET.PERSONAL_CALENDAR_EVENT_UPDATED,
        {**_invite("fresh-01"), "summary": "Renamed"},
    )
    events, _ = await _snapshot(db)
    assert (_row(events, row_id) or {}).get("summary") == "Renamed"

    await _send(
        app,
        FET.PERSONAL_CALENDAR_EVENT_DELETED,
        {"event_id": "fresh-01", "attendee_user_ids": [ANNA]},
    )
    events, _ = await _snapshot(db)
    assert _row(events, row_id) is None


async def test_the_sending_household_updates_and_deletes_a_seeded_invite(env):
    """Control: the pre-existing PEER mirror stays editable by PEER, and the
    attendee-less delete fallback still finds it."""
    app, db = env
    row_id = _mint_event_id(PEER, PEER_REMOTE, ANNA)
    await _send(
        app,
        FET.PERSONAL_CALENDAR_EVENT_UPDATED,
        {**_invite(PEER_REMOTE), "summary": "Moved"},
    )
    events, _ = await _snapshot(db)
    assert (_row(events, row_id) or {}).get("summary") == "Moved"
    await _send(app, FET.PERSONAL_CALENDAR_EVENT_DELETED, {"event_id": PEER_REMOTE})
    events, _ = await _snapshot(db)
    assert _row(events, row_id) is None
    # OTHER's mirror is untouched throughout.
    assert _row(events, _mint_event_id(OTHER, OTHER_REMOTE, ANNA)) is not None

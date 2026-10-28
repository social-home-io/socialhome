"""Regression (notifications review, live finding #3): when a space event is
moved or cancelled in ANOTHER household, the people of THIS household who
said they're going (or maybe) hear of it.

Before the fix the inbound ``SPACE_CALENDAR_EVENT_UPDATED`` published no
``CalendarEventUpdated`` at all and the inbound ``SPACE_CALENDAR_EVENT_DELETED``
published a ``CalendarEventDeleted`` with no cohort — so only the household
that made the change ever rang anyone. No new message rides the wire: each
household tells its own RSVP cohort off the update / delete it already gets.
Marked ``security``: the cohort honours blocks and never reaches another
household's people.

Real application registry, real SQLite, real ``NotificationService``.
"""

from __future__ import annotations

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, notification_delivery_key
from socialhome.domain.federation import FederationEventType

from .test_space_content_authorship import (
    _CAL,
    _END,
    _OCC,
    _SEED,
    AUTHOR,
    HOST,
    SP,
    _config,
    _deliver,
)

pytestmark = pytest.mark.security

FET = FederationEventType
EV = "ev-moved"
ACTOR = "u-a"  # AUTHOR household's member, who created and changes EV
REMOTE_GOING = "u-o"  # another household's member, going: told by their own

#: Our people and their answer (``None``: a member who never answered).
LOCAL = {
    "u-going": "going",
    "u-maybe": "maybe",
    "u-wait": "waitlist",
    "u-declined": "declined",
    "u-quiet": None,
    "u-blocker": "going",  # blocked the actor: never hears of their changes
}


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    for inst in (HOST, AUTHOR):
        await db.enqueue(
            "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
            " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
            " local_inbox_id, status, source, proto_version) VALUES(?, ?, ?, '00',"
            " '00', ?, ?, 'confirmed', 'manual', 42)",
            (inst, inst, "ab" * 32, f"https://{inst}/inbox/x", f"{inst}_local"),
        )
    await db.enqueue(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, end_dt,"
        " created_by) VALUES(?, ?, 'Flea market', ?, ?, ?)",
        (EV, SP, _OCC, _END, ACTOR),
    )
    rsvps = [(uid, s) for uid, s in LOCAL.items() if s] + [
        (REMOTE_GOING, "going"),
        (ACTOR, "going"),
    ]
    for uid, status in rsvps:
        await db.enqueue(
            "INSERT INTO space_calendar_rsvps(event_id, user_id, occurrence_at,"
            " status) VALUES(?, ?, ?, ?)",
            (EV, uid, _OCC, status),
        )
    for uid in LOCAL:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?, ?, ?)",
            (uid, uid, uid),
        )
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
            (SP, uid),
        )
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("u-blocker", ACTOR),
    )
    return app, db


async def _bells(app, ntype: str) -> dict[str, int]:
    # Rows are written by the delivery worker, after the event is handled:
    # wait for it, or "tells nobody" passes on an empty table.
    await app[notification_delivery_key].drain()
    rows = await app[db_key].fetchall(
        "SELECT user_id, COUNT(*) AS n FROM notifications WHERE type=?"
        " GROUP BY user_id",
        (ntype,),
    )
    return {r["user_id"]: r["n"] for r in rows}


def _update(**changes) -> dict:
    return {
        "id": EV,
        "summary": "Flea market",
        "created_by": ACTOR,
        "actor_user_id": ACTOR,
        **_CAL,
        **changes,
    }


async def test_a_remote_move_tells_our_people_who_are_coming(env):
    app, _db = env
    await _deliver(
        app,
        FET.SPACE_CALENDAR_EVENT_UPDATED,
        _update(start="2026-06-10T20:00:00+00:00", end="2026-06-10T21:00:00+00:00"),
        sender=AUTHOR,
    )
    assert await _bells(app, "calendar_event_updated") == {
        "u-going": 1,
        "u-maybe": 1,
        "u-wait": 1,
    }


async def test_a_remote_cosmetic_edit_tells_nobody(env):
    app, _db = env
    await _deliver(
        app,
        FET.SPACE_CALENDAR_EVENT_UPDATED,
        _update(description="bring cash"),
        sender=AUTHOR,
    )
    assert await _bells(app, "calendar_event_updated") == {}


async def test_a_remote_cancel_tells_our_people_who_were_coming(env):
    app, db = env
    await _deliver(
        app,
        FET.SPACE_CALENDAR_EVENT_DELETED,
        {"id": EV, "actor_user_id": ACTOR},
        sender=AUTHOR,
    )
    assert await _bells(app, "calendar_event_cancelled") == {
        "u-going": 1,
        "u-maybe": 1,
        "u-wait": 1,
    }
    rows = await db.fetchall(
        "SELECT title, link_url FROM notifications WHERE type='calendar_event_cancelled'",
        (),
    )
    assert {(r["title"], r["link_url"]) for r in rows} == {
        ("Event cancelled: Flea market", f"/spaces/{SP}/calendar")
    }

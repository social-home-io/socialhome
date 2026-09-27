"""Release-blocker protocol tests: a space-content write stays in its space.

Marked ``@pytest.mark.security``.

The §24.11 pipeline judges a sender against ONE space — the envelope's
routing ``space_id`` (payload copy as the fallback). The ban check and the
Follower write gate both decide "may this household write to space A?".
That answer is only worth something if the handler then writes *into
space A*: a content event names rows by id, and a row id says nothing
about which space the row lives in. So the rule these tests encode is:

    A space-content event gated for space A mutates only rows of space A.
    Naming a row of space B — or a household's own non-space row (a
    personal page, a household sticky, a household gallery album) —
    changes nothing, however the id is dressed up (as the target, as the
    parent a child row hangs from, or as the conflict an upsert lands on).

Every case below is an envelope gated for :data:`GATED` (``sp-a``) that is
otherwise perfectly valid, aimed at a row of :data:`VICTIM` (``sp-b``) or
at a household row. The assertion is deliberately blunt: a snapshot of
every content table before and after must be identical.

The tripwire at the end walks :data:`SPACE_WRITE_EVENT_TYPES` — the same
vocabulary the Follower gate uses — against the REAL application
registry. A space-content event type must either carry at least one
cross-space case here or be listed in :data:`NOT_ROW_SCOPED` with the
reason it names no row; a new handler therefore fails this file until
somebody writes its cross-space case.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    space_sync_receiver_key,
)
from socialhome.config import Config
from socialhome.domain.federation import (
    SPACE_WRITE_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
)

pytestmark = pytest.mark.security

FET = FederationEventType

GATED = "sp-a"  # the space the sender is seated in and was gated for
VICTIM = "sp-b"  # a space the sender must not be able to touch
LOCAL_USER = "u-local"  # a real local user, so FKs never do the refusing
SENDER = "peer-seated-in-a"

#: Every table holding space content, plus the household tables a
#: space-content event could otherwise reach. Snapshotted whole.
CONTENT_TABLES = (
    "space_posts",
    "space_post_comments",
    "space_task_lists",
    "space_tasks",
    "space_pages",
    "pages",
    "stickies",
    "space_calendar_events",
    "space_calendar_rsvps",
    "pending_federated_rsvps",
    "space_polls",
    "space_poll_options",
    "space_poll_votes",
    "space_schedule_poll_meta",
    "space_schedule_slots",
    "space_schedule_responses",
    "gallery_albums",
    "gallery_items",
    "space_zones",
    "bazaar_listings",
    "bazaar_bids",
)

_NOW = "2026-06-01T10:00:00+00:00"
_OCC = "2026-06-10T18:00:00+00:00"

#: Envelopes gated for ``GATED`` that aim at ``VICTIM`` / household rows.
#: ``(label, payload)`` per event type. Every payload is complete — the
#: only thing wrong with it is the space its ids belong to.
ATTACKS: dict[FederationEventType, list[tuple[str, dict]]] = {
    # ── Posts + comments ──
    FET.SPACE_POST_CREATED: [
        (
            "re-create B's post",
            {"id": "post-b", "author": "u-evil", "type": "text", "content": "x"},
        ),
    ],
    FET.SPACE_POST_UPDATED: [("edit B's post", {"id": "post-b", "content": "x"})],
    FET.SPACE_POST_DELETED: [("delete B's post", {"post_id": "post-b"})],
    FET.SPACE_COMMENT_CREATED: [
        (
            "comment on B's post",
            {
                "post_id": "post-b",
                "comment_id": "cmt-new",
                "author": "u-evil",
                "type": "text",
                "content": "x",
            },
        ),
        (
            "reply on A's post under B's comment",
            {
                "post_id": "post-a",
                "comment_id": "cmt-new",
                "parent_id": "cmt-b",
                "author": "u-evil",
                "type": "text",
                "content": "x",
            },
        ),
    ],
    FET.SPACE_COMMENT_UPDATED: [("edit B's comment", {"id": "cmt-b", "content": "x"})],
    FET.SPACE_COMMENT_DELETED: [
        ("delete B's comment", {"comment_id": "cmt-b", "post_id": "post-b"}),
    ],
    # ── Tasks ──
    FET.SPACE_TASK_CREATED: [
        (
            "file a task under B's list",
            {"id": "task-new", "list_id": "list-b", "title": "x"},
        ),
    ],
    FET.SPACE_TASK_UPDATED: [
        (
            "rewrite B's task onto A's list",
            {"id": "task-b", "list_id": "list-a", "title": "x"},
        ),
    ],
    FET.SPACE_TASK_DELETED: [("delete B's task", {"id": "task-b"})],
    # ── Pages ──
    FET.SPACE_PAGE_CREATED: [("re-create B's page", {"id": "page-b", "title": "x"})],
    FET.SPACE_PAGE_UPDATED: [("rewrite B's page", {"id": "page-b", "title": "x"})],
    FET.SPACE_PAGE_DELETED: [
        ("delete B's page", {"id": "page-b"}),
        ("delete the household's personal page", {"id": "page-home"}),
    ],
    # ── Stickies ──
    FET.SPACE_STICKY_CREATED: [
        (
            "re-create B's sticky",
            {"id": "sticky-b", "author": "u-evil", "content": "x"},
        ),
    ],
    FET.SPACE_STICKY_UPDATED: [
        (
            "rewrite the household sticky",
            {"id": "sticky-home", "author": "u-evil", "content": "x"},
        ),
    ],
    FET.SPACE_STICKY_DELETED: [
        ("delete B's sticky", {"id": "sticky-b"}),
        ("delete the household sticky", {"id": "sticky-home"}),
    ],
    # ── Calendar + RSVPs ──
    FET.SPACE_CALENDAR_EVENT_CREATED: [
        (
            "re-create B's event",
            {
                "id": "ev-b",
                "calendar_id": GATED,
                "summary": "x",
                "created_by": "u-evil",
                "start": _OCC,
                "end": "2026-06-10T19:00:00+00:00",
            },
        ),
    ],
    FET.SPACE_CALENDAR_EVENT_UPDATED: [
        (
            "rewrite B's event",
            {
                "id": "ev-b",
                "calendar_id": GATED,
                "summary": "x",
                "created_by": "u-evil",
                "start": _OCC,
                "end": "2026-06-10T19:00:00+00:00",
            },
        ),
    ],
    FET.SPACE_CALENDAR_EVENT_DELETED: [("delete B's event", {"id": "ev-b"})],
    FET.SPACE_RSVP_UPDATED: [
        (
            "RSVP to B's event",
            {
                "event_id": "ev-b",
                "user_id": "u-evil",
                "status": "going",
                "occurrence_at": _OCC,
                "updated_at": _NOW,
            },
        ),
        (
            "overwrite an RSVP on B's event",
            {
                "event_id": "ev-b",
                "user_id": "u-b",
                "status": "declined",
                "occurrence_at": _OCC,
                "updated_at": _NOW,
            },
        ),
    ],
    FET.SPACE_RSVP_DELETED: [
        (
            "drop an RSVP on B's event",
            {
                "event_id": "ev-b",
                "user_id": "u-b",
                "occurrence_at": _OCC,
                "updated_at": _NOW,
            },
        ),
    ],
    # ── Polls + schedule polls ──
    FET.SPACE_POLL_VOTE_CAST: [
        (
            "vote on B's poll",
            {"post_id": "post-b", "option_id": "opt-b", "voter_user_id": "u-evil"},
        ),
        (
            "move B's voter's vote",
            {"post_id": "post-b", "option_id": "opt-b2", "voter_user_id": "u-b"},
        ),
        (
            "vote with B's option on A's poll",
            {"post_id": "post-a", "option_id": "opt-b", "voter_user_id": "u-evil"},
        ),
    ],
    FET.SPACE_POLL_CLOSED: [("close B's poll", {"post_id": "post-b"})],
    FET.SPACE_SCHEDULE_CREATED: [
        (
            "rewrite B's schedule poll",
            {
                "post_id": "post-b",
                "title": "x",
                "slots": [{"id": "slot-b", "slot_date": "1999-01-01"}],
            },
        ),
        (
            "adopt B's slot into A's schedule poll",
            {
                "post_id": "post-a",
                "title": "When?",
                "slots": [{"id": "slot-b", "slot_date": "1999-01-01"}],
            },
        ),
    ],
    FET.SPACE_SCHEDULE_RESPONSE_UPDATED: [
        (
            "answer on B's slot",
            {"slot_id": "slot-b", "user_id": "u-evil", "response": "yes"},
        ),
        (
            "retract B's voter's answer",
            {"slot_id": "slot-b", "user_id": "u-b", "response": "retracted"},
        ),
    ],
    FET.SPACE_SCHEDULE_FINALIZED: [
        ("finalise B's schedule poll", {"post_id": "post-b", "slot_id": "slot-b"}),
        (
            "finalise A's poll on B's slot",
            {"post_id": "post-a", "slot_id": "slot-b"},
        ),
    ],
    # ── Gallery ──
    FET.SPACE_GALLERY_ITEM_CREATED: [
        (
            "upload into B's album",
            {"id": "gi-new", "album_id": "album-b", "uploaded_by": LOCAL_USER},
        ),
        (
            "upload into the household album",
            {"id": "gi-new", "album_id": "album-home", "uploaded_by": LOCAL_USER},
        ),
    ],
    FET.SPACE_GALLERY_ITEM_DELETED: [
        ("delete B's item", {"id": "gi-b"}),
        ("delete a household item", {"id": "gi-home"}),
    ],
    # ── Zones ──
    FET.SPACE_ZONE_UPSERTED: [
        (
            "rewrite B's zone",
            {
                "zone_id": "zone-b",
                "name": "Evil",
                "latitude": 1.0,
                "longitude": 1.0,
                "radius_m": 100,
            },
        ),
    ],
    FET.SPACE_ZONE_DELETED: [("delete B's zone", {"zone_id": "zone-b"})],
    # ── Bazaar ──
    FET.BAZAAR_LISTING_CREATED: [
        (
            "rewrite B's listing",
            {
                "post_id": "post-b-listing",
                "seller_user_id": "u-evil",
                "mode": "offer",
                "title": "x",
                "currency": "EUR",
                "end_time": "2099-01-01T00:00:00",
            },
        ),
        (
            "hang a listing on B's post",
            {
                "post_id": "post-b",
                "seller_user_id": "u-evil",
                "mode": "offer",
                "title": "x",
                "currency": "EUR",
                "end_time": "2099-01-01T00:00:00",
            },
        ),
    ],
    FET.BAZAAR_LISTING_UPDATED: [
        (
            "mark B's listing sold",
            {
                "post_id": "post-b-listing",
                "status": "sold",
                "winner_user_id": "u-evil",
                "winning_price": 1,
            },
        ),
        ("expire B's listing", {"post_id": "post-b-listing", "status": "expired"}),
        (
            "cancel B's listing",
            {"post_id": "post-b-listing", "status": "cancelled"},
        ),
    ],
    FET.BAZAAR_BID_PLACED: [
        (
            "bid on B's listing",
            {
                "bid_id": "bid-new",
                "listing_post_id": "post-b-listing",
                "bidder_user_id": "u-evil",
                "amount": 999,
            },
        ),
    ],
    FET.BAZAAR_OFFER_ACCEPTED: [("accept B's offer", {"bid_id": "bid-b"})],
}

#: Space-write event types that carry no row id of their own — the reason
#: is the review record. Anything else in SPACE_WRITE_EVENT_TYPES must
#: have a case in ATTACKS.
NOT_ROW_SCOPED: dict[FederationEventType, str] = {
    FET.SPACE_POLL_CREATED: (
        "no inbound handler — the poll body rides on SPACE_POST_CREATED"
    ),
    FET.SPACE_LOCATION_UPDATED: (
        "keyed on (routing space, sender's user) — names no row id; the "
        "handler already writes to the routing space (roster-authority tests)"
    ),
    FET.SPACE_MEDIA_BLOB: (
        "writes media bytes keyed by filename, not a space row — its scope "
        "and write-once rules live in test_media_blob_scope.py"
    ),
}


# ── Fixture: the real app, two spaces, one of everything ─────────────


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "scope.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


_SEED = [
    (
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("local", LOCAL_USER, "Local"),
    ),
    *[
        (
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?,?,?,?,?)",
            (sid, sid, "the-host", "anna", "00" * 32),
        )
        for sid in (GATED, VICTIM)
    ],
    # Posts: one per space (poll + schedule wrapper), one listing anchor in B.
    *[
        (
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES(?,?,?,?,?)",
            (pid, sid, "u-" + sid[-1], "poll", "body"),
        )
        for pid, sid in (
            ("post-a", GATED),
            ("post-b", VICTIM),
            ("post-b-listing", VICTIM),
        )
    ],
    (
        "INSERT INTO space_post_comments(id, post_id, author, type, content)"
        " VALUES('cmt-b', 'post-b', 'u-b', 'text', 'B comment')",
        (),
    ),
    *[
        (
            "INSERT INTO space_task_lists(id, space_id, name, created_by)"
            " VALUES(?,?,?,?)",
            (lid, sid, "L", "u-x"),
        )
        for lid, sid in (("list-a", GATED), ("list-b", VICTIM))
    ],
    (
        "INSERT INTO space_tasks(id, list_id, space_id, title, created_by)"
        " VALUES('task-b', 'list-b', ?, 'B task', 'u-b')",
        (VICTIM,),
    ),
    (
        "INSERT INTO space_pages(id, space_id, title, content, created_by)"
        " VALUES('page-b', ?, 'B page', 'body', 'u-b')",
        (VICTIM,),
    ),
    (
        "INSERT INTO pages(id, title, content, created_by)"
        " VALUES('page-home', 'Personal', 'body', ?)",
        (LOCAL_USER,),
    ),
    (
        "INSERT INTO stickies(id, space_id, author, content)"
        " VALUES('sticky-b', ?, 'u-b', 'B sticky')",
        (VICTIM,),
    ),
    (
        "INSERT INTO stickies(id, space_id, author, content)"
        " VALUES('sticky-home', NULL, ?, 'Household sticky')",
        (LOCAL_USER,),
    ),
    (
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
        " end_dt, created_by) VALUES('ev-b', ?, 'B event', ?, ?, 'u-b')",
        (VICTIM, _OCC, "2026-06-10T19:00:00+00:00"),
    ),
    (
        "INSERT INTO space_calendar_rsvps(event_id, user_id, status,"
        " occurrence_at, updated_at) VALUES('ev-b', 'u-b', 'going', ?, ?)",
        (_OCC, "2026-05-01T00:00:00+00:00"),
    ),
    *[
        (
            "INSERT INTO space_polls(post_id, question) VALUES(?, 'Q')",
            (pid,),
        )
        for pid in ("post-a", "post-b")
    ],
    *[
        (
            "INSERT INTO space_poll_options(id, post_id, text) VALUES(?,?,'Yes')",
            (oid, pid),
        )
        for oid, pid in (("opt-a", "post-a"), ("opt-b", "post-b"), ("opt-b2", "post-b"))
    ],
    (
        "INSERT INTO space_poll_votes(option_id, voter_user_id) VALUES('opt-b', 'u-b')",
        (),
    ),
    *[
        (
            "INSERT INTO space_schedule_poll_meta(post_id, title) VALUES(?, 'When?')",
            (pid,),
        )
        for pid in ("post-a", "post-b")
    ],
    *[
        (
            "INSERT INTO space_schedule_slots(id, post_id, slot_date)"
            " VALUES(?, ?, '2026-07-01')",
            (slot, pid),
        )
        for slot, pid in (("slot-a", "post-a"), ("slot-b", "post-b"))
    ],
    (
        "INSERT INTO space_schedule_responses(slot_id, user_id, availability)"
        " VALUES('slot-b', 'u-b', 'yes')",
        (),
    ),
    *[
        (
            "INSERT INTO gallery_albums(id, space_id, owner_user_id, name,"
            " item_count) VALUES(?, ?, ?, 'Album', 1)",
            (aid, sid, LOCAL_USER),
        )
        for aid, sid in (("album-b", VICTIM), ("album-home", None))
    ],
    *[
        (
            "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type,"
            " filename, thumbnail_filename, width, height)"
            " VALUES(?, ?, ?, 'photo', 'f.webp', 't.webp', 1, 1)",
            (iid, aid, LOCAL_USER),
        )
        for iid, aid in (("gi-b", "album-b"), ("gi-home", "album-home"))
    ],
    (
        "INSERT INTO space_zones(id, space_id, name, latitude, longitude,"
        " radius_m, created_by, created_at, updated_at)"
        " VALUES('zone-b', ?, 'Home', 47.1, 8.5, 100, 'u-b', ?, ?)",
        (VICTIM, _NOW, _NOW),
    ),
    (
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,"
        " title, end_time, currency) VALUES('post-b-listing', ?, 'u-b', 'offer',"
        " 'Bike', '2099-01-01T00:00:00', 'EUR')",
        (VICTIM,),
    ),
    (
        "INSERT INTO bazaar_bids(id, listing_post_id, bidder_user_id, amount)"
        " VALUES('bid-b', 'post-b-listing', 'u-b2', 50)",
        (),
    ),
]


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)  # runs the startup hooks (DB, wiring)
    db = app[db_key]
    for sql, params in _SEED:
        await db.enqueue(sql, params)
    return app, db


async def _snapshot(db) -> dict[str, list[tuple]]:
    out = {}
    for table in CONTENT_TABLES:
        rows = await db.fetchall(f"SELECT * FROM {table} ORDER BY rowid", ())
        out[table] = [tuple(r) for r in rows]
    return out


def _event(event_type, payload, *, space_id=GATED) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{event_type.value}",
        event_type=event_type,
        from_instance=SENDER,
        to_instance="us",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


_CASES = [
    pytest.param(et, payload, id=f"{et.value}: {label}")
    for et, cases in ATTACKS.items()
    for label, payload in cases
]


# ── Behaviour: every registered handler, every case ─────────────────


@pytest.mark.parametrize(("event_type", "payload"), _CASES)
async def test_a_write_gated_for_one_space_cannot_touch_another(
    env, event_type, payload
):
    """Invoke EVERY handler the real registry binds to the event type —
    ``dispatch`` runs them all, so an unguarded sibling of a guarded
    handler would be caught here — and require the content tables to be
    byte-for-byte unchanged."""
    app, db = env
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers, f"no handler registered for {event_type.value}"
    before = await _snapshot(db)
    for handler in handlers:
        await handler(_event(event_type, dict(payload)))
    after = await _snapshot(db)
    changed = {
        t: (before[t], after[t]) for t in CONTENT_TABLES if before[t] != after[t]
    }
    assert not changed, f"{event_type.value} wrote across the space boundary: {changed}"


@pytest.mark.parametrize(("event_type", "payload"), _CASES)
async def test_a_payload_naming_another_space_is_refused_outright(
    env, event_type, payload
):
    """The payload's own ``space_id`` cannot redirect a write the routing
    field gated for another space — a mismatch is a refusal, not a
    tiebreak."""
    app, db = env
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    before = await _snapshot(db)
    for handler in handlers:
        await handler(_event(event_type, dict(payload, space_id=VICTIM)))
    assert await _snapshot(db) == before


async def test_the_victims_rows_are_still_writable_by_their_own_space(env):
    """Control: the same envelopes gated for the victim's own space DO land
    — the refusals above are about the space, not a broken handler."""
    app, db = env
    registry = app[federation_service_key]._event_registry
    for event_type, payload in (
        (FET.SPACE_POST_UPDATED, {"id": "post-b", "content": "edited"}),
        (FET.SPACE_ZONE_DELETED, {"zone_id": "zone-b"}),
        (FET.SPACE_POLL_CLOSED, {"post_id": "post-b"}),
        (FET.SPACE_GALLERY_ITEM_DELETED, {"id": "gi-b"}),
        (
            FET.BAZAAR_LISTING_UPDATED,
            {"post_id": "post-b-listing", "status": "cancelled"},
        ),
    ):
        for handler in registry.handlers_for(event_type):
            await handler(_event(event_type, payload, space_id=VICTIM))
    post = await db.fetchone("SELECT content FROM space_posts WHERE id='post-b'", ())
    assert post["content"] == "edited"
    assert await db.fetchone("SELECT 1 FROM space_zones WHERE id='zone-b'", ()) is None
    poll = await db.fetchone(
        "SELECT closed FROM space_polls WHERE post_id='post-b'", ()
    )
    assert poll["closed"] == 1
    assert await db.fetchone("SELECT 1 FROM gallery_items WHERE id='gi-b'", ()) is None
    listing = await db.fetchone(
        "SELECT status FROM bazaar_listings WHERE post_id='post-b-listing'", ()
    )
    assert listing["status"] == "cancelled"


# ── The §25.6 catch-up stream is held to the same rule ───────────────


_SYNC_ATTACKS = [
    ("posts", [{"id": "post-b", "author": "u-evil", "type": "text", "content": "x"}]),
    (
        "comments",
        [
            {
                "id": "cmt-new",
                "post_id": "post-b",
                "author": "u-evil",
                "type": "text",
                "content": "x",
            }
        ],
    ),
    (
        "tasks",
        [
            {"id": "task-b", "list_id": "list-a", "title": "x", "created_by": "u"},
            {"id": "task-new", "list_id": "list-b", "title": "x", "created_by": "u"},
        ],
    ),
    ("pages", [{"id": "page-b", "title": "x", "created_by": "u"}]),
    (
        "stickies",
        [
            {"id": "sticky-home", "author": "u", "content": "x"},
            {"id": "sticky-b", "author": "u", "content": "x"},
        ],
    ),
    (
        "comments",
        [
            {
                "id": "cmt-reply",
                "post_id": "post-a",
                "parent_id": "cmt-b",
                "author": "u-evil",
                "type": "text",
                "content": "x",
            }
        ],
    ),
    (
        "calendar",
        [
            {
                "id": "ev-b",
                "calendar_id": GATED,
                "summary": "x",
                "created_by": "u",
                "start": _OCC,
                "end": "2026-06-10T19:00:00+00:00",
            }
        ],
    ),
    (
        "gallery",
        [
            {
                "kind": "item",
                "id": "gi-new",
                "album_id": "album-b",
                "uploaded_by": LOCAL_USER,
            },
            {
                "kind": "item",
                "id": "gi-new2",
                "album_id": "album-home",
                "uploaded_by": LOCAL_USER,
            },
        ],
    ),
    (
        "schedules",
        [
            {
                "post_id": "post-b",
                "title": "x",
                "slots": [{"id": "slot-b", "slot_date": "1999-01-01"}],
            }
        ],
    ),
    (
        "space_zones",
        [
            {
                "id": "zone-b",
                "name": "Evil",
                "latitude": 1.0,
                "longitude": 1.0,
                "radius_m": 100,
                "created_by": "u",
            }
        ],
    ),
    (
        "bazaar",
        [
            {
                "post_id": "post-b-listing",
                "seller_user_id": "u-evil",
                "mode": "offer",
                "title": "x",
                "currency": "EUR",
                "end_time": "2099-01-01T00:00:00",
            }
        ],
    ),
]


@pytest.mark.parametrize(
    ("resource", "records"),
    [pytest.param(r, recs, id=r) for r, recs in _SYNC_ATTACKS],
)
async def test_a_sync_stream_for_one_space_cannot_write_another(env, resource, records):
    """A decrypted, verified sync chunk for ``GATED`` whose records name
    ``VICTIM`` / household rows changes nothing."""
    app, db = env
    receiver = app[space_sync_receiver_key]
    before = await _snapshot(db)
    await receiver._dispatch(resource, GATED, records)
    assert await _snapshot(db) == before


# ── The tripwire ─────────────────────────────────────────────────────


def test_every_space_content_event_type_has_a_cross_space_case():
    """A space-write event type with no cross-space case is a handler
    nobody proved stays inside its space. Write the case (or, if the type
    genuinely names no row, record why in NOT_ROW_SCOPED)."""
    uncovered = SPACE_WRITE_EVENT_TYPES - ATTACKS.keys() - NOT_ROW_SCOPED.keys()
    assert not uncovered, sorted(t.value for t in uncovered)
    assert not (ATTACKS.keys() & NOT_ROW_SCOPED.keys())
    assert ATTACKS.keys() <= SPACE_WRITE_EVENT_TYPES


async def test_every_attacked_type_is_really_handled(env):
    """Guard against a vacuous pass: every attacked type has a handler in
    the real registry, and every row-scoped exemption is not silently a
    content handler in disguise."""
    app, _db = env
    registry = app[federation_service_key]._event_registry
    unhandled = [t.value for t in ATTACKS if not registry.handlers_for(t)]
    assert not unhandled
    assert not registry.handlers_for(FET.SPACE_POLL_CREATED)

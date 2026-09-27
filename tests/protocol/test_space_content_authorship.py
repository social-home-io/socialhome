"""Release-blocker protocol tests: a household writes only for its own members.

Marked ``@pytest.mark.security``.

``test_space_content_scope.py`` proves a write gated for space A stays in
space A. This file proves the other half: *inside* one space, the users a
payload names — the author of a post, the voter on a poll, the seller of a
listing — must be members seated on the household that signed the
envelope. The rule per event family (see ``federation/space_authorship.py``):

* **create** — the claimed author is seated on the sender (or the space
  host relays a *remote* member's row, the §25.6 replay); never a local
  user of ours;
* **owned edit / delete** — the row's owner is seated on the sender, or
  the sender moderates the space (the host, or a household holding an
  ``admin`` seat) — the federated form of "author or space admin";
* **personal action** (vote, RSVP, schedule answer, bid) and **owner-only
  state change** (close a poll, finalise a schedule, settle a listing,
  accept an offer) — strictly the named user's own household;
* **collaborative** (pages, stickies, calendar events — any member may edit
  them locally) — any writer household, attribution preserved;
* **zones** — moderators only (the local service is admin-only).

Every case runs against the REAL application registry over a real SQLite
database and compares a snapshot of every content table: a refused case
must leave it byte-for-byte unchanged, an allowed one must change it. The
allowed cases are the positive controls — they are what makes a refusal
meaningful rather than a handler that silently does nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.federation import (
    SPACE_WRITE_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
)

from .test_space_content_scope import ATTACKS, CONTENT_TABLES, NOT_ROW_SCOPED

pytestmark = pytest.mark.security

FET = FederationEventType

SP = "sp-shared"
HOST = "the-host"  # hosts SP; its own member u-h is mirrored here
AUTHOR = "house-author"  # u-a's household — every seeded row is u-a's
OTHER = "house-other"  # a member household, seated as u-o
ADMIN = "house-admin"  # a household holding a live admin seat (u-adm)
STRANGER = "house-stranger"  # a household with no seat in SP at all
THIRD = "house-third"  # a member household uninvolved in the row (u-t)
LOCAL_USER = "u-local"  # one of OUR users — a remote household never acts for it

_NOW = "2026-06-01T10:00:00+00:00"
_OCC = "2026-06-10T18:00:00+00:00"
_END = "2026-06-10T19:00:00+00:00"
_FAR = "2099-01-01T00:00:00"


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "authorship.db"),
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
    # ``gallery_items.uploaded_by`` is FK'd to ``users``, so the gallery
    # cases use u-g, who has a users row (to keep the FK from doing the
    # refusing) AND a seat on AUTHOR — the seat is what the rule reads.
    *[
        (
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (name, uid, name),
        )
        for name, uid in (("local", LOCAL_USER), ("ug", "u-g"))
    ],
    (
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (SP, SP, HOST, "anna", "00" * 32),
    ),
    *[
        (
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES(?,?,?,?)",
            (SP, inst, uid, role),
        )
        for inst, uid, role in (
            (HOST, "u-h", "member"),
            (AUTHOR, "u-a", "member"),
            (AUTHOR, "u-g", "member"),
            (AUTHOR, "u-w", "member"),
            (OTHER, "u-o", "member"),
            (ADMIN, "u-adm", "admin"),
            (THIRD, "u-t", "member"),
        )
    ],
    # Posts: poll/schedule wrapper, a bazaar anchor with listing, a bare
    # bazaar anchor, a bare schedule anchor, and one by our local user.
    *[
        (
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES(?,?,?,?,?)",
            (pid, SP, author, ptype, "body"),
        )
        for pid, author, ptype in (
            ("post-a", "u-a", "poll"),
            ("post-a-listing", "u-a", "bazaar"),
            ("post-a-bare", "u-a", "bazaar"),
            ("post-a-sched", "u-a", "schedule"),
            ("post-l", LOCAL_USER, "text"),
        )
    ],
    *[
        (
            "INSERT INTO space_post_comments(id, post_id, author, type, content)"
            " VALUES(?, ?, ?, 'text', 'c')",
            (cid, pid, author),
        )
        for cid, pid, author in (
            ("cmt-a", "post-a", "u-a"),
            ("cmt-l", "post-l", LOCAL_USER),
        )
    ],
    (
        "INSERT INTO space_task_lists(id, space_id, name, created_by)"
        " VALUES('list-a', ?, 'L', 'u-a')",
        (SP,),
    ),
    (
        "INSERT INTO space_tasks(id, list_id, space_id, title, created_by)"
        " VALUES('task-a', 'list-a', ?, 'A task', 'u-a')",
        (SP,),
    ),
    (
        "INSERT INTO space_pages(id, space_id, title, content, created_by)"
        " VALUES('page-a', ?, 'A page', 'body', 'u-a')",
        (SP,),
    ),
    (
        "INSERT INTO stickies(id, space_id, author, content)"
        " VALUES('sticky-a', ?, 'u-a', 'A sticky')",
        (SP,),
    ),
    (
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
        " end_dt, created_by) VALUES('ev-a', ?, 'A event', ?, ?, 'u-a')",
        (SP, _OCC, _END),
    ),
    (
        "INSERT INTO space_calendar_rsvps(event_id, user_id, status,"
        " occurrence_at, updated_at) VALUES('ev-a', 'u-a', 'maybe', ?, ?)",
        (_OCC, "2026-05-01T00:00:00+00:00"),
    ),
    # A capacity-limited event by u-a: u-o has asked to join, u-w (a
    # household-a member) waits for a seat, nobody is going yet.
    (
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
        " end_dt, created_by, capacity) VALUES('ev-cap', ?, 'Cap', ?, ?, 'u-a', 1)",
        (SP, _OCC, _END),
    ),
    *[
        (
            "INSERT INTO space_calendar_rsvps(event_id, user_id, status,"
            " occurrence_at, updated_at) VALUES('ev-cap', ?, ?, ?, ?)",
            (uid, status, _OCC, "2026-05-01T00:00:00+00:00"),
        )
        for uid, status in (("u-o", "requested"), ("u-w", "waitlist"))
    ],
    ("INSERT INTO space_polls(post_id, question) VALUES('post-a', 'Q')", ()),
    (
        "INSERT INTO space_poll_options(id, post_id, text)"
        " VALUES('opt-a', 'post-a', 'Yes')",
        (),
    ),
    (
        "INSERT INTO space_schedule_poll_meta(post_id, title)"
        " VALUES('post-a', 'When?')",
        (),
    ),
    (
        "INSERT INTO space_schedule_slots(id, post_id, slot_date)"
        " VALUES('slot-a', 'post-a', '2026-07-01')",
        (),
    ),
    (
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name,"
        " item_count) VALUES('album-a', ?, ?, 'Album', 1)",
        (SP, LOCAL_USER),
    ),
    (
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type,"
        " filename, thumbnail_filename, width, height)"
        " VALUES('gi-a', 'album-a', 'u-g', 'photo', 'f.webp', 't.webp', 1, 1)",
        (),
    ),
    (
        "INSERT INTO space_zones(id, space_id, name, latitude, longitude,"
        " radius_m, created_by, created_at, updated_at)"
        " VALUES('zone-a', ?, 'Home', 47.1, 8.5, 100, 'u-adm', ?, ?)",
        (SP, _NOW, _NOW),
    ),
    (
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,"
        " title, end_time, currency) VALUES('post-a-listing', ?, 'u-a', 'offer',"
        " 'Bike', ?, 'EUR')",
        (SP, _FAR),
    ),
    (
        "INSERT INTO bazaar_bids(id, listing_post_id, bidder_user_id, amount)"
        " VALUES('bid-o', 'post-a-listing', 'u-o', 50)",
        (),
    ),
]


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
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


def _event(event_type, payload, *, sender) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{event_type.value}-{sender}",
        event_type=event_type,
        from_instance=sender,
        to_instance="us",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=dict(payload),
        space_id=SP,
    )


async def _deliver(app, event_type, payload, *, sender) -> None:
    registry = app[federation_service_key]._event_registry
    handlers = registry.handlers_for(event_type)
    assert handlers, f"no handler registered for {event_type.value}"
    for handler in handlers:
        await handler(_event(event_type, payload, sender=sender))


_CAL = {"calendar_id": SP, "start": _OCC, "end": _END}

#: ``(event_type, label, payload, allowed senders, refused senders)``.
#: Every allowed sender is a positive control run on a fresh database.
CASES: list[tuple[FederationEventType, str, dict, tuple[str, ...], tuple[str, ...]]] = [
    # ── Creates: the claimed author must be seated on the sender ──
    (
        FET.SPACE_POST_CREATED,
        "post as u-a",
        {"id": "post-new", "author": "u-a", "type": "text", "content": "x"},
        (AUTHOR, HOST),
        (OTHER, ADMIN, STRANGER),
    ),
    (
        FET.SPACE_POST_CREATED,
        "post as our local user",
        {"id": "post-new", "author": LOCAL_USER, "type": "text", "content": "x"},
        (),
        (AUTHOR, OTHER, ADMIN, HOST),
    ),
    (
        FET.SPACE_POST_CREATED,
        "re-create u-a's post under another author",
        {"id": "post-a", "author": "u-o", "type": "text", "content": "hijack"},
        (),
        (OTHER, AUTHOR),
    ),
    (
        FET.SPACE_POST_CREATED,
        "re-create u-a's post as u-a",
        {"id": "post-a", "author": "u-a", "type": "text", "content": "hijack"},
        (AUTHOR,),
        (OTHER, ADMIN),
    ),
    (
        FET.SPACE_COMMENT_CREATED,
        "comment as u-a",
        {
            "post_id": "post-a",
            "comment_id": "cmt-new",
            "author": "u-a",
            "type": "text",
            "content": "x",
        },
        (AUTHOR, HOST),
        (OTHER, ADMIN, STRANGER),
    ),
    (
        FET.SPACE_TASK_CREATED,
        "task by u-a",
        {"id": "task-new", "list_id": "list-a", "title": "x", "created_by": "u-a"},
        (AUTHOR, HOST),
        (OTHER, ADMIN),
    ),
    (
        FET.SPACE_PAGE_CREATED,
        "page by u-a",
        {"id": "page-new", "title": "x", "created_by": "u-a"},
        (AUTHOR, HOST),
        (OTHER, ADMIN),
    ),
    (
        FET.SPACE_STICKY_CREATED,
        "sticky by u-a",
        {"id": "sticky-new", "author": "u-a", "content": "x"},
        (AUTHOR, HOST),
        (OTHER, ADMIN),
    ),
    (
        FET.SPACE_CALENDAR_EVENT_CREATED,
        "event by u-a",
        {"id": "ev-new", "summary": "x", "created_by": "u-a", **_CAL},
        (AUTHOR, HOST),
        (OTHER, ADMIN),
    ),
    (
        FET.SPACE_GALLERY_ITEM_CREATED,
        "upload by u-g",
        {"id": "gi-new", "album_id": "album-a", "uploaded_by": "u-g"},
        (AUTHOR,),
        (OTHER, ADMIN),
    ),
    (
        FET.BAZAAR_LISTING_CREATED,
        "list u-a's anchor as u-a",
        {
            "post_id": "post-a-bare",
            "seller_user_id": "u-a",
            "mode": "offer",
            "title": "x",
            "currency": "EUR",
            "end_time": _FAR,
        },
        (AUTHOR,),
        (OTHER, ADMIN),
    ),
    (
        FET.BAZAAR_LISTING_CREATED,
        "hang u-o's listing on u-a's anchor",
        {
            "post_id": "post-a-bare",
            "seller_user_id": "u-o",
            "mode": "offer",
            "title": "x",
            "currency": "EUR",
            "end_time": _FAR,
        },
        (),
        (OTHER,),
    ),
    (
        FET.BAZAAR_LISTING_CREATED,
        "take over u-a's existing listing",
        {
            "post_id": "post-a-listing",
            "seller_user_id": "u-o",
            "mode": "offer",
            "title": "mine now",
            "currency": "EUR",
            "end_time": _FAR,
        },
        (),
        (OTHER,),
    ),
    (
        FET.SPACE_SCHEDULE_CREATED,
        "schedule on u-a's post",
        {
            "post_id": "post-a-sched",
            "title": "When?",
            "slots": [{"id": "slot-new", "slot_date": "2026-08-01"}],
        },
        (AUTHOR,),
        (OTHER, ADMIN),
    ),
    # ── Personal actions: strictly the named user's own household ──
    (
        FET.SPACE_POLL_VOTE_CAST,
        "vote as u-a",
        {"post_id": "post-a", "option_id": "opt-a", "voter_user_id": "u-a"},
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    (
        FET.SPACE_RSVP_UPDATED,
        "RSVP as u-a",
        {
            "event_id": "ev-a",
            "user_id": "u-a",
            "status": "going",
            "occurrence_at": _OCC,
            "updated_at": _NOW,
        },
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    (
        FET.SPACE_RSVP_UPDATED,
        "buffer an RSVP as u-a for an event not here yet",
        {
            "event_id": "ev-later",
            "user_id": "u-a",
            "status": "going",
            "occurrence_at": _OCC,
            "updated_at": _NOW,
        },
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    (
        FET.SPACE_RSVP_DELETED,
        "drop u-a's RSVP",
        {
            "event_id": "ev-a",
            "user_id": "u-a",
            "occurrence_at": _OCC,
            "updated_at": _NOW,
        },
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    # The organiser settles somebody else's request (approve / deny), and
    # any writer household promotes a waitlisted member into a free seat —
    # the two cross-household RSVP writes the calendar service makes.
    (
        FET.SPACE_RSVP_UPDATED,
        "approve u-o's request on u-a's event",
        {
            "event_id": "ev-cap",
            "user_id": "u-o",
            "status": "going",
            "occurrence_at": _OCC,
            "updated_at": _NOW,
        },
        (AUTHOR, ADMIN, HOST, OTHER),
        (THIRD, STRANGER),
    ),
    (
        FET.SPACE_RSVP_DELETED,
        "deny u-o's request on u-a's event",
        {
            "event_id": "ev-cap",
            "user_id": "u-o",
            "occurrence_at": _OCC,
            "updated_at": _NOW,
        },
        (AUTHOR, ADMIN, OTHER),
        (THIRD,),
    ),
    (
        FET.SPACE_RSVP_UPDATED,
        "promote waitlisted u-w into the free seat",
        {
            "event_id": "ev-cap",
            "user_id": "u-w",
            "status": "going",
            "occurrence_at": _OCC,
            "updated_at": _NOW,
        },
        (THIRD, OTHER, AUTHOR),
        (STRANGER,),
    ),
    (
        FET.SPACE_RSVP_UPDATED,
        "decline for somebody else's waitlisted member",
        {
            "event_id": "ev-cap",
            "user_id": "u-w",
            "status": "declined",
            "occurrence_at": _OCC,
            "updated_at": _NOW,
        },
        (AUTHOR,),
        (THIRD, ADMIN, HOST),
    ),
    (
        FET.SPACE_SCHEDULE_RESPONSE_UPDATED,
        "answer as u-a",
        {"slot_id": "slot-a", "user_id": "u-a", "response": "yes"},
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    (
        FET.BAZAAR_BID_PLACED,
        "bid as u-o",
        {
            "bid_id": "bid-new",
            "listing_post_id": "post-a-listing",
            "bidder_user_id": "u-o",
            "amount": 60,
        },
        (OTHER,),
        (AUTHOR, ADMIN, HOST),
    ),
    # ── Owner-only state changes ──
    (
        FET.SPACE_POLL_CLOSED,
        "close u-a's poll",
        {"post_id": "post-a"},
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    (
        FET.SPACE_SCHEDULE_FINALIZED,
        "finalise u-a's schedule",
        {"post_id": "post-a", "slot_id": "slot-a"},
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    (
        FET.BAZAAR_LISTING_UPDATED,
        "cancel u-a's listing",
        {"post_id": "post-a-listing", "status": "cancelled"},
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    (
        FET.BAZAAR_LISTING_UPDATED,
        "mark u-a's listing sold",
        {
            "post_id": "post-a-listing",
            "status": "sold",
            "winner_user_id": "u-o",
            "winning_price": 50,
        },
        (AUTHOR,),
        (OTHER, ADMIN),
    ),
    (
        FET.BAZAAR_OFFER_ACCEPTED,
        "accept an offer on u-a's listing",
        {"bid_id": "bid-o"},
        (AUTHOR,),
        (OTHER, ADMIN, HOST),
    ),
    # ── Owned rows: the owner's household or a moderator ──
    (
        FET.SPACE_POST_UPDATED,
        "edit u-a's post",
        {"id": "post-a", "content": "edited"},
        (AUTHOR, ADMIN, HOST),
        (OTHER, STRANGER),
    ),
    (
        FET.SPACE_POST_UPDATED,
        "edit our local user's post",
        {"id": "post-l", "content": "edited"},
        (ADMIN, HOST),
        (AUTHOR, OTHER),
    ),
    (
        FET.SPACE_POST_DELETED,
        "delete u-a's post",
        {"post_id": "post-a"},
        (AUTHOR, ADMIN, HOST),
        (OTHER, STRANGER),
    ),
    (
        FET.SPACE_POST_DELETED,
        "delete our local user's post",
        {"post_id": "post-l"},
        (ADMIN, HOST),
        (AUTHOR, OTHER),
    ),
    (
        FET.SPACE_COMMENT_UPDATED,
        "edit u-a's comment",
        {"id": "cmt-a", "content": "edited"},
        (AUTHOR, ADMIN, HOST),
        (OTHER,),
    ),
    (
        FET.SPACE_COMMENT_DELETED,
        "delete u-a's comment",
        {"comment_id": "cmt-a", "post_id": "post-a"},
        (AUTHOR, ADMIN, HOST),
        (OTHER,),
    ),
    (
        FET.SPACE_COMMENT_DELETED,
        "delete our local user's comment",
        {"comment_id": "cmt-l", "post_id": "post-l"},
        (ADMIN,),
        (AUTHOR, OTHER),
    ),
    (
        FET.SPACE_TASK_UPDATED,
        "edit u-a's task",
        {"id": "task-a", "list_id": "list-a", "title": "edited", "created_by": "u-a"},
        (AUTHOR, ADMIN, HOST),
        (OTHER,),
    ),
    (
        FET.SPACE_TASK_DELETED,
        "delete u-a's task",
        {"id": "task-a"},
        (AUTHOR, ADMIN, HOST),
        (OTHER,),
    ),
    (
        FET.SPACE_GALLERY_ITEM_DELETED,
        "delete u-g's upload",
        {"id": "gi-a"},
        (AUTHOR, ADMIN, HOST),
        (OTHER,),
    ),
    # ── Collaborative rows: any writer household; attribution is kept ──
    (
        FET.SPACE_PAGE_UPDATED,
        "edit u-a's page",
        {"id": "page-a", "title": "edited", "created_by": "u-o"},
        (AUTHOR, OTHER, ADMIN),
        (STRANGER,),
    ),
    (
        FET.SPACE_PAGE_DELETED,
        "delete u-a's page",
        {"id": "page-a"},
        (AUTHOR, OTHER),
        (STRANGER,),
    ),
    (
        FET.SPACE_STICKY_UPDATED,
        "edit u-a's sticky",
        {"id": "sticky-a", "content": "edited", "color": "blue"},
        (AUTHOR, OTHER),
        (STRANGER,),
    ),
    (
        FET.SPACE_STICKY_DELETED,
        "delete u-a's sticky",
        {"id": "sticky-a"},
        (AUTHOR, OTHER),
        (STRANGER,),
    ),
    (
        FET.SPACE_CALENDAR_EVENT_UPDATED,
        "edit u-a's event",
        {"id": "ev-a", "summary": "edited", "created_by": "u-o", **_CAL},
        (AUTHOR, OTHER),
        (STRANGER,),
    ),
    (
        FET.SPACE_CALENDAR_EVENT_DELETED,
        "delete u-a's event",
        {"id": "ev-a"},
        (AUTHOR, OTHER),
        (STRANGER,),
    ),
    # ── Zones: moderators only ──
    (
        FET.SPACE_ZONE_UPSERTED,
        "move the zone",
        {
            "zone_id": "zone-a",
            "name": "Moved",
            "latitude": 1.0,
            "longitude": 1.0,
            "radius_m": 100,
        },
        (ADMIN, HOST),
        (AUTHOR, OTHER),
    ),
    (
        FET.SPACE_ZONE_UPSERTED,
        "add a zone",
        {
            "zone_id": "zone-new",
            "name": "New",
            "latitude": 1.0,
            "longitude": 1.0,
            "radius_m": 100,
            "created_by": "u-adm",
        },
        (ADMIN,),
        (AUTHOR, OTHER),
    ),
    (
        FET.SPACE_ZONE_DELETED,
        "delete the zone",
        {"zone_id": "zone-a"},
        (ADMIN, HOST),
        (AUTHOR, OTHER),
    ),
]

_ALLOWED = [
    pytest.param(et, payload, sender, id=f"{et.value}: {label} <- {sender}")
    for et, label, payload, allowed, _refused in CASES
    for sender in allowed
]
_REFUSED = [
    pytest.param(et, payload, sender, id=f"{et.value}: {label} <- {sender}")
    for et, label, payload, _allowed, refused in CASES
    for sender in refused
]


@pytest.mark.parametrize(("event_type", "payload", "sender"), _REFUSED)
async def test_a_household_cannot_write_for_somebody_elses_member(
    env, event_type, payload, sender
):
    app, db = env
    before = await _snapshot(db)
    await _deliver(app, event_type, payload, sender=sender)
    after = await _snapshot(db)
    changed = {t for t in CONTENT_TABLES if before[t] != after[t]}
    assert not changed, f"{event_type.value} from {sender} wrote {sorted(changed)}"


@pytest.mark.parametrize(("event_type", "payload", "sender"), _ALLOWED)
async def test_the_rightful_household_still_writes(env, event_type, payload, sender):
    """Positive control for every refusal above — same space, same row,
    the household that may act: the write lands."""
    app, db = env
    before = await _snapshot(db)
    await _deliver(app, event_type, payload, sender=sender)
    after = await _snapshot(db)
    assert before != after, f"{event_type.value} from {sender} changed nothing"


async def test_collaborative_edits_keep_the_original_attribution(env):
    """A co-member edits u-a's page and event and claims ``created_by``:
    the edit lands, the attribution does not move."""
    app, db = env
    await _deliver(
        app,
        FET.SPACE_PAGE_UPDATED,
        {"id": "page-a", "title": "edited", "created_by": "u-o"},
        sender=OTHER,
    )
    await _deliver(
        app,
        FET.SPACE_CALENDAR_EVENT_UPDATED,
        {"id": "ev-a", "summary": "edited", "created_by": "u-o", **_CAL},
        sender=OTHER,
    )
    page = await db.fetchone(
        "SELECT title, created_by FROM space_pages WHERE id='page-a'", ()
    )
    assert (page["title"], page["created_by"]) == ("edited", "u-a")
    ev = await db.fetchone(
        "SELECT summary, created_by FROM space_calendar_events WHERE id='ev-a'", ()
    )
    assert (ev["summary"], ev["created_by"]) == ("edited", "u-a")


async def test_a_squatted_buffered_rsvp_cannot_drop_the_real_one(env):
    """A buffered RSVP is keyed on (event, user, occurrence). Only the
    user's own household may buffer for that user, so the key cannot be
    squatted from another household — and a later write from that same
    household (e.g. for the space the event really lands in) wins."""
    app, db = env
    rsvp = {
        "event_id": "ev-later",
        "user_id": "u-a",
        "status": "declined",
        "occurrence_at": _OCC,
        "updated_at": _NOW,
    }
    await _deliver(app, FET.SPACE_RSVP_UPDATED, rsvp, sender=OTHER)
    assert await db.fetchone("SELECT 1 FROM pending_federated_rsvps", ()) is None, (
        "another household buffered an RSVP for u-a"
    )
    await _deliver(
        app, FET.SPACE_RSVP_UPDATED, dict(rsvp, status="going"), sender=AUTHOR
    )
    row = await db.fetchone(
        "SELECT status, space_id FROM pending_federated_rsvps"
        " WHERE event_id='ev-later' AND user_id='u-a'",
        (),
    )
    assert (row["status"], row["space_id"]) == ("going", SP)


# ── The tripwire ─────────────────────────────────────────────────────


def test_every_space_content_event_type_has_an_authorship_case():
    """Each row-writing space event type has at least one allowed case
    (positive control) and one refused case in this matrix — including
    every type the cross-space matrix attacks."""
    allowed = {et for et, _l, _p, a, _r in CASES if a}
    refused = {et for et, _l, _p, _a, r in CASES if r}
    expected = SPACE_WRITE_EVENT_TYPES - NOT_ROW_SCOPED.keys()
    assert not expected - allowed, sorted(t.value for t in expected - allowed)
    assert not expected - refused, sorted(t.value for t in expected - refused)
    assert ATTACKS.keys() <= allowed

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
from socialhome.app_keys import (
    db_key,
    event_bus_key,
    federation_service_key,
    space_sync_receiver_key,
)
from socialhome.config import Config
from socialhome.crypto import generate_space_keypair
from socialhome.domain.events import SpaceRemoteSeatLive
from socialhome.domain.federation import (
    SPACE_WRITE_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
)
from socialhome.federation.inbound_validator import (
    InboundContext,
    run_post_decrypt_gates,
)
from socialhome.federation.sync.space.exporter import ALLOWED_RESOURCES
from socialhome.services.space_crypto_service import (
    sign_authority_event,
    strip_authority_sig_fields,
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
    # u-g is the gallery cases' member: seated on AUTHOR and, like every
    # remote member, with no ``users`` row here — the gallery columns carry
    # no FK to it (migration 0046), and a users row would make u-g one of
    # OUR people, whose rows the host may never relay.
    (
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("local", LOCAL_USER, "local"),
    ),
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
            (AUTHOR, "u-sub", "subscriber"),
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
            ("post-bot", "system-integration", "text"),
        )
    ],
    # A moderator-deleted post and a moderation-held post by u-a, and
    # state on post-a (pinned, reactions, a comment count) that no
    # re-send may reset.
    (
        "INSERT INTO space_posts(id, space_id, author, type, content, deleted)"
        " VALUES('post-del', ?, 'u-a', 'text', NULL, 1)",
        (SP,),
    ),
    (
        "INSERT INTO space_posts(id, space_id, author, type, content, moderated)"
        " VALUES('post-mod', ?, 'u-a', 'text', 'held', 1)",
        (SP,),
    ),
    (
        "UPDATE space_posts SET pinned=1, comment_count=3,"
        " reactions='{\"👍\": [\"u-o\"]}' WHERE id='post-a'",
        (),
    ),
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
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name,"
        " item_count) VALUES('album-g', ?, 'u-g', 'Theirs', 0)",
        (SP,),
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
        (AUTHOR, ADMIN, HOST),
        (OTHER, STRANGER),
    ),
    (
        FET.SPACE_POST_CREATED,
        "re-send a bot post",
        {
            "id": "post-bot",
            "author": "system-integration",
            "type": "text",
            "content": "rewritten",
        },
        (ADMIN, HOST),
        (OTHER, AUTHOR, STRANGER),
    ),
    (
        FET.SPACE_POST_CREATED,
        "re-send a deleted post",
        {"id": "post-del", "author": "u-a", "type": "text", "content": "back"},
        (),
        (AUTHOR, ADMIN, HOST, OTHER),
    ),
    (
        FET.SPACE_POST_CREATED,
        "re-send a moderation-held post",
        {"id": "post-mod", "author": "u-a", "type": "text", "content": "unheld"},
        (ADMIN, HOST),
        (AUTHOR, OTHER),
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
        (AUTHOR, HOST),
        (OTHER, ADMIN),
    ),
    (
        FET.SPACE_GALLERY_ALBUM_CREATED,
        "album by u-g",
        {"id": "album-new", "owner_user_id": "u-g", "name": "Trip"},
        (AUTHOR, HOST),
        (OTHER, ADMIN),
    ),
    (
        FET.SPACE_GALLERY_ALBUM_CREATED,
        "album by our local user",
        {"id": "album-new", "owner_user_id": LOCAL_USER, "name": "Trip"},
        (),
        (AUTHOR, HOST, ADMIN),
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
        {"id": "task-a", "list_id": "list-a", "title": "edited", "created_by": "u-o"},
        (AUTHOR, OTHER, ADMIN, HOST),
        (STRANGER,),
    ),
    (
        FET.SPACE_TASK_DELETED,
        "delete u-a's task",
        {"id": "task-a"},
        (AUTHOR, OTHER, ADMIN, HOST),
        (STRANGER,),
    ),
    (
        FET.SPACE_GALLERY_ITEM_DELETED,
        "delete u-g's upload",
        {"id": "gi-a"},
        (AUTHOR, ADMIN, HOST),
        (OTHER,),
    ),
    (
        FET.SPACE_GALLERY_ALBUM_UPDATED,
        "rename u-g's album",
        {"id": "album-g", "name": "Renamed"},
        (AUTHOR, ADMIN, HOST),
        (OTHER, STRANGER),
    ),
    (
        FET.SPACE_GALLERY_ALBUM_UPDATED,
        "rename our local user's album",
        {"id": "album-a", "name": "Renamed"},
        (ADMIN, HOST),
        (AUTHOR, OTHER),
    ),
    (
        FET.SPACE_GALLERY_ALBUM_DELETED,
        "delete u-g's album",
        {"id": "album-g"},
        (AUTHOR, ADMIN, HOST),
        (OTHER, STRANGER),
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


async def test_a_remote_members_new_album_and_its_upload_both_land(env):
    """Regression: a member of another household makes an album after this
    household joined and uploads into it. Before v_33 only the item was
    federated, it named an album nobody else held, and every other member
    household refused it — the bytes arrived, the gallery stayed empty."""
    app, db = env
    item = {
        "id": "gi-late",
        "album_id": "album-late",
        "uploaded_by": "u-g",
        "thumbnail_url": "/api/media/t.webp",
        "width": 4,
        "height": 3,
    }
    await _deliver(app, FET.SPACE_GALLERY_ITEM_CREATED, item, sender=AUTHOR)
    assert (
        await db.fetchone("SELECT 1 FROM gallery_items WHERE id='gi-late'", ()) is None
    )
    await _deliver(
        app,
        FET.SPACE_GALLERY_ALBUM_CREATED,
        {"id": "album-late", "owner_user_id": "u-g", "name": "Late"},
        sender=AUTHOR,
    )
    await _deliver(app, FET.SPACE_GALLERY_ITEM_CREATED, item, sender=AUTHOR)
    album = await db.fetchone(
        "SELECT space_id, owner_user_id, item_count FROM gallery_albums"
        " WHERE id='album-late'",
        (),
    )
    assert (album["space_id"], album["owner_user_id"], album["item_count"]) == (
        SP,
        "u-g",
        1,
    )
    row = await db.fetchone(
        "SELECT album_id, uploaded_by FROM gallery_items WHERE id='gi-late'", ()
    )
    assert (row["album_id"], row["uploaded_by"]) == ("album-late", "u-g")


async def test_an_album_id_held_for_one_owner_is_never_claimed_for_another(env, caplog):
    """An album create naming an id this household already holds is only
    ever a redelivery of that album. Claimed for a different owner it is
    refused — loudly — and the stored owner stands, so the claimant gains
    no owner rights (rename, cover, delete-with-items) over it."""
    app, db = env
    with caplog.at_level("WARNING"):
        await _deliver(
            app,
            FET.SPACE_GALLERY_ALBUM_CREATED,
            {"id": "album-g", "owner_user_id": "u-o", "name": "Mine now"},
            sender=OTHER,
        )
    row = await db.fetchone(
        "SELECT owner_user_id, name FROM gallery_albums WHERE id='album-g'", ()
    )
    assert (row["owner_user_id"], row["name"]) == ("u-g", "Theirs")
    assert "already held for another owner" in caplog.text
    await _deliver(
        app,
        FET.SPACE_GALLERY_ALBUM_UPDATED,
        {"id": "album-g", "name": "Renamed by the claimant"},
        sender=OTHER,
    )
    await _deliver(
        app, FET.SPACE_GALLERY_ALBUM_DELETED, {"id": "album-g"}, sender=OTHER
    )
    row = await db.fetchone("SELECT name FROM gallery_albums WHERE id='album-g'", ())
    assert row is not None and row["name"] == "Theirs"


async def test_a_redelivered_album_from_its_owners_household_is_quiet(env, caplog):
    app, db = env
    with caplog.at_level("WARNING"):
        await _deliver(
            app,
            FET.SPACE_GALLERY_ALBUM_CREATED,
            {"id": "album-g", "owner_user_id": "u-g", "name": "Theirs"},
            sender=AUTHOR,
        )
    assert "album-g" not in caplog.text


async def test_an_album_delete_that_overtakes_its_create_keeps_it_deleted(env):
    app, db = env
    await _deliver(
        app, FET.SPACE_GALLERY_ALBUM_DELETED, {"id": "album-racy"}, sender=AUTHOR
    )
    await _deliver(
        app,
        FET.SPACE_GALLERY_ALBUM_CREATED,
        {"id": "album-racy", "owner_user_id": "u-g", "name": "Late"},
        sender=AUTHOR,
    )
    assert (
        await db.fetchone("SELECT 1 FROM gallery_albums WHERE id='album-racy'", ())
        is None
    )


async def test_federated_gallery_deletes_remove_the_files(env, tmp_dir):
    """An item delete takes its files; an album delete takes every item's
    files — each unless another row still names it."""
    app, db = env
    media = tmp_dir / "media"
    media.mkdir(exist_ok=True)
    await _deliver(
        app,
        FET.SPACE_GALLERY_ALBUM_CREATED,
        {"id": "album-files", "owner_user_id": "u-g", "name": "Files"},
        sender=AUTHOR,
    )
    for n in (1, 2):
        for kind in ("full", "thumb"):
            (media / f"{kind}{n}.webp").write_bytes(b"x")
        await _deliver(
            app,
            FET.SPACE_GALLERY_ITEM_CREATED,
            {
                "id": f"gi-files-{n}",
                "album_id": "album-files",
                "uploaded_by": "u-g",
                "url": f"api/media/full{n}.webp",
                "thumbnail_url": f"api/media/thumb{n}.webp",
            },
            sender=AUTHOR,
        )
    (media / "f.webp").write_bytes(b"x")  # gi-a's file, still referenced
    await _deliver(
        app, FET.SPACE_GALLERY_ITEM_DELETED, {"id": "gi-files-1"}, sender=AUTHOR
    )
    assert not (media / "full1.webp").exists()
    assert not (media / "thumb1.webp").exists()
    assert (media / "full2.webp").exists()
    await _deliver(
        app, FET.SPACE_GALLERY_ALBUM_DELETED, {"id": "album-files"}, sender=AUTHOR
    )
    assert not (media / "full2.webp").exists()
    assert not (media / "thumb2.webp").exists()
    assert (media / "f.webp").exists()


async def test_a_synced_album_and_upload_of_a_remote_member_both_land(env):
    """§25.6 sync receiver, real SQLite: the host streams an album owned by
    a member of another household and that member's upload into it. Neither
    user has a ``users`` row here; both rows must land (#650)."""
    app, db = env
    receiver = app[space_sync_receiver_key]
    await receiver._dispatch(
        "gallery",
        SP,
        [
            {
                "kind": "album",
                "id": "album-sync",
                "owner_user_id": "u-g",
                "name": "Synced",
                "item_count": 1,
            },
            {
                "kind": "item",
                "id": "gi-sync-g",
                "album_id": "album-sync",
                "uploaded_by": "u-g",
                "thumbnail_url": "/api/media/t.webp",
                "width": 1,
                "height": 1,
            },
        ],
        provider=HOST,
    )
    album = await db.fetchone(
        "SELECT owner_user_id FROM gallery_albums WHERE id='album-sync'", ()
    )
    assert album is not None and album["owner_user_id"] == "u-g"
    item = await db.fetchone(
        "SELECT uploaded_by FROM gallery_items WHERE id='gi-sync-g'", ()
    )
    assert item is not None and item["uploaded_by"] == "u-g"


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


async def test_a_re_send_never_resets_a_posts_state(env):
    """A re-sent ``SPACE_POST_CREATED`` (resume replay, or the author's own
    household) may refresh the content; it never touches the state other
    people put on the row — pin, reactions, comment count, the moderation
    hold."""
    app, db = env
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "post-a", "author": "u-a", "type": "text", "content": "fresh"},
        sender=AUTHOR,
    )
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "post-mod", "author": "u-a", "type": "text", "content": "fresh"},
        sender=HOST,
    )
    a = await db.fetchone(
        "SELECT content, pinned, comment_count, reactions, deleted, moderated"
        " FROM space_posts WHERE id='post-a'",
        (),
    )
    assert a["content"] == "fresh"
    assert (a["pinned"], a["comment_count"], a["deleted"], a["moderated"]) == (
        1,
        3,
        0,
        0,
    )
    assert "u-o" in a["reactions"]
    held = await db.fetchone(
        "SELECT content, moderated FROM space_posts WHERE id='post-mod'", ()
    )
    assert (held["content"], held["moderated"]) == ("fresh", 1)


# ── Subscriber seats and the host relay ──────────────────────────────


async def test_a_read_only_seat_cannot_author(env):
    """A household that also holds a Follower seat may not post as that
    follower — a read-only seat authors nothing."""
    app, db = env
    before = await _snapshot(db)
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "p-sub", "author": "u-sub", "type": "text", "content": "x"},
        sender=AUTHOR,
    )
    assert await _snapshot(db) == before


async def test_a_follower_may_comment_only_with_the_opt_in(env):
    app, db = env
    comment = {
        "post_id": "post-a",
        "comment_id": "c-sub",
        "author": "u-sub",
        "type": "text",
        "content": "x",
    }
    await _deliver(app, FET.SPACE_COMMENT_CREATED, comment, sender=AUTHOR)
    assert (
        await db.fetchone("SELECT 1 FROM space_post_comments WHERE id='c-sub'", ())
        is None
    )
    await db.enqueue("UPDATE spaces SET allow_subscriber_comment=1 WHERE id=?", (SP,))
    await _deliver(app, FET.SPACE_COMMENT_CREATED, comment, sender=AUTHOR)
    assert await db.fetchone("SELECT 1 FROM space_post_comments WHERE id='c-sub'", ())


async def test_the_host_does_not_relay_a_user_the_space_never_had(env):
    app, db = env
    before = await _snapshot(db)
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "p-ghost", "author": "u-never", "type": "text", "content": "x"},
        sender=HOST,
    )
    assert await _snapshot(db) == before


# ── Writes that beat the roster (held, then replayed) ────────────────


async def _seat_lands(app, db, instance_id, user_id, role="member"):
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?,?,?,?)",
        (SP, instance_id, user_id, role),
    )
    await app[event_bus_key].publish(
        SpaceRemoteSeatLive(space_id=SP, instance_id=instance_id, user_id=user_id)
    )


async def test_a_post_by_a_member_not_yet_in_the_roster_lands_with_the_seat(env):
    """u-new joined household-a; its first post beats the gossip seating it.
    The post is held, not dropped, and lands once the seat does."""
    app, db = env
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "p-early", "author": "u-new", "type": "text", "content": "hi"},
        sender=AUTHOR,
    )
    assert await db.fetchone("SELECT 1 FROM space_posts WHERE id='p-early'", ()) is None
    await _seat_lands(app, db, AUTHOR, "u-new")
    row = await db.fetchone("SELECT author FROM space_posts WHERE id='p-early'", ())
    assert row is not None and row["author"] == "u-new"


async def test_a_write_from_a_household_not_yet_seated_is_held_at_the_gate(env):
    """A household that just joined writes before any seat for it reached
    us: the write gate holds it and the seat releases it through the same
    gates and handlers."""
    app, db = env
    fed = app[federation_service_key]
    event = _event(
        FET.SPACE_POST_CREATED,
        {"id": "p-joiner", "author": "u-j", "type": "text", "content": "hi"},
        sender="house-joiner",
    )
    ctx = InboundContext(
        envelope={"space_id": SP, "from_instance": "house-joiner"}, event=event
    )
    assert not await run_post_decrypt_gates(
        ctx, steps=fed.post_decrypt_gate_steps(include_ban_check=True)
    )
    assert ctx.early_response == {"status": "ok", "held": "awaiting-seat"}
    await _seat_lands(app, db, "house-joiner", "u-j")
    assert await db.fetchone("SELECT 1 FROM space_posts WHERE id='p-joiner'", ())


async def test_a_held_write_is_still_refused_if_the_seat_is_someone_elses(env):
    """Holding is for "we have not heard of this user yet" only: when the
    seat that lands puts the user on ANOTHER household, the replay is
    refused like any foreign-author write."""
    app, db = env
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "p-spoof", "author": "u-late", "type": "text", "content": "x"},
        sender=OTHER,
    )
    await _seat_lands(app, db, AUTHOR, "u-late")
    assert await db.fetchone("SELECT 1 FROM space_posts WHERE id='p-spoof'", ()) is None


async def test_an_upgraded_household_with_an_empty_mirror_heals_from_a_snapshot(
    env,
):
    """A household whose mirror never received the gossip for household-a's
    new member gets the host's roster snapshot; the member's held post then
    lands."""
    app, db = env
    kp = generate_space_keypair()
    await db.enqueue(
        "UPDATE spaces SET identity_public_key=? WHERE id=?", (kp.public_key.hex(), SP)
    )
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "p-heal", "author": "u-heal", "type": "text", "content": "x"},
        sender=AUTHOR,
    )
    bare = {
        "space_id": SP,
        "user_id": "u-heal",
        "instance_id": AUTHOR,
        "display_name": "Heal",
        "user_pk": None,
        "role": "member",
        "member_version": 7,
        "roster_version": 7,
    }
    signed = {
        **bare,
        **sign_authority_event(
            event_type=FET.SPACE_MEMBER_JOINED.value,
            space_id=SP,
            payload=strip_authority_sig_fields(bare),
            space_seed=kp.private_key,
        ),
    }
    await _deliver(
        app,
        FET.SPACE_ROSTER_SNAPSHOT,
        {
            "space_id": SP,
            "entries": [
                {"event_type": FET.SPACE_MEMBER_JOINED.value, "payload": signed}
            ],
        },
        sender=HOST,
    )
    assert await db.fetchone(
        "SELECT 1 FROM space_remote_members WHERE user_id='u-heal'", ()
    )
    assert await db.fetchone("SELECT 1 FROM space_posts WHERE id='p-heal'", ())


# ── The §25.6 catch-up stream from a household that is not the host ──

#: Tables the catch-up stream can write, roster + bans included.
_SYNC_TABLES = (
    *CONTENT_TABLES,
    "space_members",
    "space_bans",
    "space_member_profile_pictures",
)


async def _sync_snapshot(db) -> dict[str, list[tuple]]:
    out = {}
    for table in _SYNC_TABLES:
        rows = await db.fetchall(f"SELECT * FROM {table} ORDER BY rowid", ())
        out[table] = [tuple(r) for r in rows]
    return out


#: ``(resource, label, records, allowed providers, refused providers)``.
#: The host's stream is taken whole; any other provider may only ADD rows,
#: each attributed to a member seated on it.
SYNC_CASES: list[tuple[str, str, list, tuple[str, ...], tuple[str, ...]]] = [
    (
        "members",
        "seat somebody",
        [{"user_id": "u-evil", "role": "admin", "joined_at": _NOW}],
        (HOST,),
        (AUTHOR, ADMIN),
    ),
    (
        "bans",
        "ban u-a",
        [{"user_id": "u-a", "banned_by": "u-o"}],
        (HOST,),
        (OTHER, ADMIN),
    ),
    (
        "member_pictures",
        "set u-a's picture",
        [
            {
                "user_id": "u-a",
                "picture_webp_base64": "UklGRg==",
                "picture_hash": "h1",
                "width": 1,
                "height": 1,
            }
        ],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "posts",
        "a new post by u-a",
        [{"id": "p-sync", "author": "u-a", "type": "text", "content": "x"}],
        (AUTHOR, HOST),
        (OTHER, ADMIN),
    ),
    (
        "posts",
        "overwrite (and un-moderate) an existing post",
        [
            {
                "id": "post-mod",
                "author": "u-a",
                "type": "text",
                "content": "rewritten",
                "moderated": False,
            }
        ],
        (HOST,),
        (AUTHOR, OTHER, ADMIN),
    ),
    (
        "comments",
        "a new comment by u-a",
        [
            {
                "id": "c-sync",
                "post_id": "post-a",
                "author": "u-a",
                "type": "text",
                "content": "x",
            }
        ],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "tasks",
        "a new task by u-a",
        [{"id": "t-sync", "list_id": "list-a", "title": "x", "created_by": "u-a"}],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "tasks_archived",
        "rewrite u-a's task",
        [{"id": "task-a", "list_id": "list-a", "title": "rw", "created_by": "u-a"}],
        (HOST,),
        (AUTHOR, OTHER),
    ),
    (
        "pages",
        "a new page by u-a",
        [{"id": "pg-sync", "title": "x", "created_by": "u-a"}],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "stickies",
        "a new sticky by u-a",
        [{"id": "st-sync", "author": "u-a", "content": "x"}],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "calendar",
        "a new event by u-a",
        [{"id": "ev-sync", "summary": "x", "created_by": "u-a", **_CAL}],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "gallery",
        "an upload by u-g",
        [
            {
                "kind": "item",
                "id": "gi-sync",
                "album_id": "album-a",
                "uploaded_by": "u-g",
            }
        ],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "schedules",
        "slots on u-a's schedule post",
        [
            {
                "post_id": "post-a-sched",
                "title": "When?",
                "slots": [{"id": "slot-sync", "slot_date": "2026-08-01"}],
            }
        ],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "space_zones",
        "move the zone",
        [
            {
                "id": "zone-a",
                "name": "Moved",
                "latitude": 2.0,
                "longitude": 2.0,
                "radius_m": 50,
                "created_by": "u-adm",
            }
        ],
        (ADMIN, HOST),
        (AUTHOR, OTHER),
    ),
    (
        "bazaar",
        "list u-a's bare anchor",
        [
            {
                "post_id": "post-a-bare",
                "seller_user_id": "u-a",
                "mode": "offer",
                "title": "x",
                "currency": "EUR",
                "end_time": _FAR,
            }
        ],
        (AUTHOR, HOST),
        (OTHER,),
    ),
    (
        "bazaar",
        "reset u-a's listing",
        [
            {
                "post_id": "post-a-listing",
                "seller_user_id": "u-a",
                "mode": "offer",
                "title": "reset",
                "currency": "EUR",
                "end_time": _FAR,
                "status": "active",
            }
        ],
        (HOST,),
        (AUTHOR, OTHER),
    ),
]

#: Resources a provider streams that write nothing, with the reason.
_SYNC_NOT_WRITTEN = {"polls": "informational — polls ride on the posts stream"}


@pytest.mark.parametrize(
    ("resource", "records", "provider"),
    [
        pytest.param(r, recs, prov, id=f"{r}: {label} <- {prov}")
        for r, label, recs, _allowed, refused in SYNC_CASES
        for prov in refused
    ],
)
async def test_a_member_households_sync_stream_cannot_write_for_others(
    env, resource, records, provider
):
    app, db = env
    before = await _sync_snapshot(db)
    await app[space_sync_receiver_key]._dispatch(
        resource, SP, [dict(r) for r in records], provider=provider
    )
    after = await _sync_snapshot(db)
    changed = {t for t in _SYNC_TABLES if before[t] != after[t]}
    assert not changed, f"{resource} from {provider} wrote {sorted(changed)}"


@pytest.mark.parametrize(
    ("resource", "records", "provider"),
    [
        pytest.param(r, recs, prov, id=f"{r}: {label} <- {prov}")
        for r, label, recs, allowed, _refused in SYNC_CASES
        for prov in allowed
    ],
)
async def test_the_rightful_sync_stream_still_writes(env, resource, records, provider):
    app, db = env
    before = await _sync_snapshot(db)
    await app[space_sync_receiver_key]._dispatch(
        resource, SP, [dict(r) for r in records], provider=provider
    )
    assert await _sync_snapshot(db) != before, f"{resource} from {provider}"


def test_every_sync_resource_has_an_authorship_case():
    covered = {r for r, *_ in SYNC_CASES}
    refused = {r for r, _l, _rec, _a, ref in SYNC_CASES if ref}
    expected = set(ALLOWED_RESOURCES) - _SYNC_NOT_WRITTEN.keys()
    assert not expected - covered, sorted(expected - covered)
    assert not expected - refused, sorted(expected - refused)

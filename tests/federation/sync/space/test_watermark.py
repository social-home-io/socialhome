"""Unit tests for :mod:`socialhome.federation.sync.space.watermark` — when a
periodic session may stream only the changed rows, and when it must not."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.conversation import ConversationMessage, SystemChatScope
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.space import SpaceSyncWatermark
from socialhome.federation.sync.space.exporter import IncrementalExporter
from socialhome.federation.sync.space.exporters import (
    BazaarExporter,
    CalendarDeletedExporter,
    CalendarExporter,
    ChatMessagesDeletedExporter,
    ChatMessagesExporter,
    CommentsDeletedExporter,
    CommentsExporter,
    GalleryAlbumsDeletedExporter,
    GalleryExporter,
    GalleryItemsDeletedExporter,
    PollsExporter,
    PostsDeletedExporter,
    PostsExporter,
    SchedulesExporter,
    PagesDeletedExporter,
    PagesExporter,
    StickiesDeletedExporter,
    StickiesExporter,
    TaskListsDeletedExporter,
    TaskListsExporter,
    TasksArchivedExporter,
    TasksDeletedExporter,
    TasksExporter,
    TimetablesExporter,
    ZonesDeletedExporter,
    ZonesExporter,
)
from socialhome.domain.page import Page
from socialhome.domain.task import Task, TaskList, TaskStatus
from socialhome.domain.timetable import Timetable
from socialhome.repositories.page_repo import SqlitePageRepo
from socialhome.repositories.task_repo import SqliteSpaceTaskRepo
from socialhome.repositories.timetable_repo import SqliteSpaceTimetableRepo
from socialhome.federation.sync.space.watermark import (
    FULL_RESYNC_INTERVAL_S,
    SYNC_SHAPE_VERSION,
    SyncWatermarks,
    parse_have_seq,
    session_shape,
)
from socialhome.federation.sync.space.window import SyncWindows
from socialhome.repositories.bazaar_repo import SqliteBazaarRepo
from socialhome.repositories.calendar_repo import SqliteSpaceCalendarRepo
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.space_poll_repo import SqliteSpacePollRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.space_zone_repo import SqliteSpaceZoneRepo
from socialhome.repositories.sticky_repo import SqliteStickyRepo

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class _Repo:
    def __init__(self, seq: int = 0, wm: SpaceSyncWatermark | None = None) -> None:
        self.seq = seq
        self.wm = wm
        self.confirmed: list[tuple] = []

    async def current_seq(self) -> int:
        return self.seq

    async def get(self, space_id: str, instance_id: str):
        return self.wm

    async def confirm(self, space_id, instance_id, *, seq, shape, full_at):
        self.confirmed.append((space_id, instance_id, seq, shape, full_at))


def _wm(*, seq=7, shape="S", age_s: float = 60.0) -> SpaceSyncWatermark:
    return SpaceSyncWatermark(
        seq=seq, shape=shape, full_at=(NOW - timedelta(seconds=age_s)).isoformat()
    )


def _marks(repo: _Repo) -> SyncWatermarks:
    return SyncWatermarks(repo, clock=lambda: NOW)


def test_the_shape_names_everything_a_stream_depends_on():
    shape = session_shape(
        peer_version=55, retention="30:event", resources=("bans", "posts")
    )
    assert shape == f"v{SYNC_SHAPE_VERSION}|p55|r30:event|bans,posts"
    assert shape != session_shape(
        peer_version=56, retention="30:event", resources=("bans", "posts")
    )
    assert shape != session_shape(
        peer_version=55, retention="", resources=("bans", "posts")
    )
    assert shape != session_shape(
        peer_version=55,
        retention="30:event",
        resources=("bans", "posts", "chat_messages"),
    )


async def test_snapshot_reads_the_counter():
    assert await _marks(_Repo(seq=42)).snapshot() == 42


async def test_an_incremental_session_with_a_fresh_matching_watermark_streams_since_it():
    since = await _marks(_Repo(wm=_wm())).since_for(
        space_id="sp",
        instance_id="h",
        shape="S",
        sync_mode="incremental",
        have_seq=100,
    )
    assert since == 7


@pytest.mark.parametrize("mode", ["initial", "full"])
async def test_every_other_session_streams_in_full(mode):
    since = await _marks(_Repo(wm=_wm())).since_for(
        space_id="sp", instance_id="h", shape="S", sync_mode=mode, have_seq=100
    )
    assert since is None


@pytest.mark.parametrize(
    "wm",
    [
        None,  # never confirmed / seat dropped / older requester
        _wm(shape="OLD"),  # upgrade, chat gate, retention change
        _wm(age_s=FULL_RESYNC_INTERVAL_S + 1),  # daily anti-entropy
        SpaceSyncWatermark(seq=7, shape="S", full_at=None),
        SpaceSyncWatermark(seq=7, shape="S", full_at="not a date"),
    ],
)
async def test_fails_safe_toward_a_full_stream(wm):
    since = await _marks(_Repo(wm=wm)).since_for(
        space_id="sp",
        instance_id="h",
        shape="S",
        sync_mode="incremental",
        have_seq=100,
    )
    assert since is None


async def test_a_naive_full_at_reads_as_utc():
    wm = SpaceSyncWatermark(seq=3, shape="S", full_at="2026-10-09 11:00:00")
    since = await _marks(_Repo(wm=wm)).since_for(
        space_id="sp",
        instance_id="h",
        shape="S",
        sync_mode="incremental",
        have_seq=100,
    )
    assert since == 3


# ── The requester's echo (have_seq, migration 0087) ───────────────────────


async def _since(have_seq, wm=None):
    return await _marks(_Repo(wm=wm or _wm(seq=7))).since_for(
        space_id="sp",
        instance_id="h",
        shape="S",
        sync_mode="incremental",
        have_seq=have_seq,
    )


async def test_a_lower_have_seq_wins_a_rolled_back_requester_re_streams_the_gap():
    assert await _since(3) == 3


async def test_a_have_seq_above_the_watermark_is_clamped_never_trusted_upward():
    assert await _since(10**12) == 7
    assert await _since(7) == 7


async def test_a_begin_without_have_seq_streams_in_full():
    assert await _since(None) is None


async def test_have_seq_never_rescues_an_untrusted_watermark():
    assert await _since(3, wm=_wm(shape="OLD")) is None


@pytest.mark.parametrize(
    ("wire", "parsed"),
    [
        (0, 0),
        (12, 12),
        (None, None),
        (-1, None),
        (True, None),
        (False, None),
        ("12", None),
        (1.0, None),
        ([], None),
    ],
)
def test_parse_have_seq_accepts_only_a_non_negative_int(wire, parsed):
    assert parse_have_seq(wire) == parsed


async def test_confirm_records_a_full_stream_with_its_time():
    repo = _Repo()
    await _marks(repo).confirm(
        space_id="sp", instance_id="h", seq=9, shape="S", full=True
    )
    assert repo.confirmed == [("sp", "h", 9, "S", NOW.isoformat())]


async def test_confirm_keeps_the_last_full_time_for_an_incremental_stream():
    repo = _Repo()
    await _marks(repo).confirm(
        space_id="sp", instance_id="h", seq=9, shape="S", full=False
    )
    assert repo.confirmed == [("sp", "h", 9, "S", None)]


# ── Tripwire: an exporter's record shape is pinned to SYNC_SHAPE_VERSION ──


async def _covered_record_keys(db) -> dict[str, list[str]]:
    """One row of every covered resource, exported; each record's keys."""
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    posts = SqliteSpacePostRepo(db)
    polls = SqliteSpacePollRepo(db)
    gallery = SqliteGalleryRepo(db)
    cal = SqliteSpaceCalendarRepo(db)
    stickies = SqliteStickyRepo(db)
    zones = SqliteSpaceZoneRepo(db)
    convos = SqliteConversationRepo(db)
    spaces = SqliteSpaceRepo(db)
    windows = SyncWindows(spaces)
    now = datetime.now(timezone.utc)
    for pid in ("p-live", "p-gone"):
        await posts.save(
            "sp",
            Post(id=pid, author="u", type=PostType.TEXT, created_at=now, content="x"),
        )
    await polls.create_poll(
        post_id="p-live",
        question="Q?",
        closes_at=None,
        allow_multiple=False,
        options=[{"id": "o", "text": "A"}],
    )
    await db.enqueue(
        "INSERT INTO space_schedule_poll_meta(post_id, title) VALUES('p-live','T')"
    )
    await db.enqueue(
        "INSERT INTO space_schedule_slots(id, post_id, slot_date)"
        " VALUES('s','p-live','2026-01-01')"
    )
    await db.enqueue(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,"
        " title, end_time, currency) VALUES('p-live','sp','u','fixed','B','2027','EUR')"
    )
    for cid in ("k-live", "k-gone"):
        await posts.add_comment(
            Comment(
                id=cid,
                post_id="p-live",
                author="u",
                type=CommentType.TEXT,
                created_at=now,
                content="c",
            ),
            space_id="sp",
        )
    await posts.soft_delete("p-gone", space_id="sp")
    await posts.soft_delete_comment("k-gone", space_id="sp")
    await db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name)"
        " VALUES('al','sp','u','A'), ('al-gone','sp','u','G')"
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height) VALUES('it','al','u','photo','f','t',1,1),"
        " ('it-gone','al','u','photo','g','h',1,1)"
    )
    await db.enqueue(
        "UPDATE gallery_items SET deleted_at='2026', deleted_by='u' WHERE id='it-gone'"
    )
    await db.enqueue(
        "UPDATE gallery_albums SET deleted_at='2026', deleted_by='u' WHERE id='al-gone'"
    )
    start = now.replace(microsecond=0).isoformat()
    await db.enqueue(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, end_dt,"
        " created_by) VALUES('ev','sp','E',?,?,'u'), ('ev-gone','sp','G',?,?,'u')",
        (start, start, start, start),
    )
    await db.enqueue(
        "UPDATE space_calendar_events SET deleted_at='2026', deleted_by='u'"
        " WHERE id='ev-gone'"
    )
    sticky = await stickies.add(author="u", content="n", space_id="sp")
    await stickies.delete(sticky.id, space_id="sp", deleted_by="u")
    await stickies.add(author="u", content="live", space_id="sp")
    await db.enqueue(
        "INSERT INTO space_zones(id, space_id, name, latitude, longitude, radius_m,"
        " created_by) VALUES('z-live','sp','Live',1,2,100,'u')"
    )
    tasks = SqliteSpaceTaskRepo(db)
    for lid in ("l-live", "l-gone"):
        await tasks.save_list(TaskList(id=lid, name=lid, created_by="u"), space_id="sp")
    await tasks.delete_list("l-gone", space_id="sp", deleted_by="u")
    for tid, archived in (("t-live", None), ("t-arch", now), ("t-gone", None)):
        await tasks.save(
            Task(
                id=tid,
                list_id="l-live",
                title=tid,
                status=TaskStatus.TODO,
                position=0,
                created_by="u",
                created_at=now,
                updated_at=now,
                archived_at=archived,
            ),
            space_id="sp",
        )
    await tasks.delete("t-gone", space_id="sp", deleted_by="u")
    pages = SqlitePageRepo(db)
    for pid in ("pg-live", "pg-gone"):
        await pages.save(
            Page(
                id=pid,
                title=pid,
                content="c",
                created_by="u",
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
                space_id="sp",
            ),
            space_id="sp",
        )
    await pages.delete("pg-gone", space_id="sp", deleted_by="u")
    timetables = SqliteSpaceTimetableRepo(db)
    await timetables.insert(
        Timetable(id="tt", name="T", created_by="u", created_at=now, updated_at=now),
        space_id="sp",
    )
    await db.enqueue(
        "INSERT INTO space_zones(id, space_id, name, latitude, longitude, radius_m,"
        " created_by, deleted_at, deleted_by) VALUES('z','sp','Z',1,2,100,'u','2026','u')"
    )
    chat = await convos.create_system_chat(SystemChatScope.SPACE, space_id="sp")
    for mid in ("m-live", "m-gone"):
        await convos.save_message(
            ConversationMessage(
                id=mid,
                conversation_id=chat.id,
                sender_user_id="u",
                content="hi",
                created_at=now,
            )
        )
    await convos.soft_delete_message("m-gone")

    exporters = [
        PostsExporter(posts, windows),
        PostsDeletedExporter(posts),
        CommentsExporter(posts, windows),
        CommentsDeletedExporter(posts),
        PollsExporter(polls, posts, windows),
        SchedulesExporter(polls, posts, windows),
        BazaarExporter(SqliteBazaarRepo(db), windows),
        GalleryExporter(gallery),
        GalleryAlbumsDeletedExporter(gallery),
        GalleryItemsDeletedExporter(gallery),
        CalendarExporter(cal),
        CalendarDeletedExporter(cal),
        StickiesDeletedExporter(stickies),
        StickiesExporter(stickies),
        ZonesDeletedExporter(zones),
        ZonesExporter(zones),
        TaskListsExporter(tasks),
        TaskListsDeletedExporter(tasks),
        TasksExporter(tasks),
        TasksArchivedExporter(tasks),
        TasksDeletedExporter(tasks),
        PagesExporter(pages),
        PagesDeletedExporter(pages),
        TimetablesExporter(timetables),
        ChatMessagesExporter(convos, spaces),
        ChatMessagesDeletedExporter(convos, spaces),
    ]
    keys: dict[str, list[str]] = {}
    for exporter in exporters:
        assert isinstance(exporter, IncrementalExporter), exporter
        records = await exporter.list_records("sp")
        assert records, exporter.resource
        for record in records:
            name = exporter.resource
            if name == "gallery":
                name = f"gallery.{record['kind']}"
            keys[name] = sorted(record)
    return keys


#: The record keys of every covered (incremental) resource at
#: ``SYNC_SHAPE_VERSION``. A changed record shape must bump the version —
#: rows stamped before the change would otherwise never re-stream in the new
#: shape until the next daily full pass.
RECORD_KEYS_AT_SHAPE: dict[str, list[str]] = {
    "bazaar": [
        "created_at",
        "currency",
        "description",
        "end_time",
        "image_urls",
        "mode",
        "post_id",
        "price",
        "seller_user_id",
        "sold_at",
        "space_id",
        "start_price",
        "status",
        "step_price",
        "title",
        "winner_user_id",
        "winning_price",
    ],
    "calendar": [
        "all_day",
        "announce_in_feed",
        "attendees",
        "calendar_id",
        "capacity",
        "client_event_uuid",
        "cover_url",
        "created_by",
        "description",
        "end",
        "id",
        "location",
        "mirrored_from",
        "origin",
        "remote_event_id",
        "remote_instance_id",
        "rrule",
        "rsvp_enabled",
        "start",
        "summary",
        "tz",
    ],
    "calendar_deleted": ["actor_user_id", "created_at", "created_by", "id"],
    "chat_messages": [
        "author_user_id",
        "content",
        "created_at",
        "id",
        "message_id",
        "reply_to_id",
    ],
    "chat_messages_deleted": ["author_user_id", "id", "message_id"],
    "comments": [
        "author",
        "children",
        "content",
        "created_at",
        "deleted",
        "edited_at",
        "id",
        "media_url",
        "parent_id",
        "post_id",
        "type",
    ],
    "comments_deleted": ["author", "comment_id", "created_at", "id", "post_id"],
    "gallery.album": [
        "cover_item_id",
        "cover_url",
        "created_at",
        "description",
        "id",
        "is_system",
        "item_count",
        "kind",
        "name",
        "owner_user_id",
        "retention_exempt",
        "space_id",
        "updated_at",
    ],
    "gallery.item": [
        "album_id",
        "caption",
        "created_at",
        "duration_s",
        "height",
        "id",
        "item_type",
        "kind",
        "sort_order",
        "source_post_id",
        "taken_at",
        "thumbnail_url",
        "uploaded_by",
        "url",
        "width",
    ],
    "gallery_albums_deleted": ["actor_user_id", "created_at", "id", "owner_user_id"],
    "gallery_items_deleted": [
        "actor_user_id",
        "album_id",
        "created_at",
        "id",
        "uploaded_by",
    ],
    "polls": ["meta", "options", "post_id"],
    "posts": [
        "author",
        "bot_id",
        "comment_count",
        "content",
        "created_at",
        "deleted",
        "edited_at",
        "file_meta",
        "hidden_from_feed",
        "id",
        "image_urls",
        "link_preview",
        "linked_event_id",
        "linked_highlight_id",
        "location",
        "media_url",
        "moderated",
        "no_link_preview",
        "pinned",
        "reactions",
        "type",
    ],
    "posts_deleted": ["author", "created_at", "id", "moderated", "post_id", "type"],
    "schedules": ["deadline", "post_id", "slots", "title"],
    "space_zones_deleted": ["actor_user_id", "created_at", "created_by", "id"],
    "stickies_deleted": ["actor_user_id", "author", "created_at", "id"],
    "pages": [
        "conflict",
        "content",
        "cover_image_url",
        "created_at",
        "created_by",
        "delete_approved_at",
        "delete_approved_by",
        "delete_requested_at",
        "delete_requested_by",
        "id",
        "last_edited_at",
        "last_editor_user_id",
        "lock_expires_at",
        "locked_at",
        "locked_by",
        "seq",
        "space_id",
        "title",
        "updated_at",
        "version_hash",
    ],
    "pages_deleted": ["actor_user_id", "created_by", "id", "page_id", "space_id"],
    "space_zones": [
        "color",
        "created_at",
        "created_by",
        "id",
        "latitude",
        "longitude",
        "name",
        "radius_m",
        "space_id",
        "updated_at",
    ],
    "stickies": [
        "author",
        "color",
        "content",
        "created_at",
        "id",
        "position_x",
        "position_y",
        "space_id",
        "updated_at",
    ],
    "task_lists": ["created_by", "id", "name", "space_id"],
    "task_lists_deleted": ["actor_user_id", "created_by", "id", "space_id"],
    "tasks": [
        "archived_at",
        "assignees",
        "created_at",
        "created_by",
        "description",
        "due_date",
        "id",
        "labels",
        "list_id",
        "position",
        "priority",
        "recurrence",
        "recurrence_parent_id",
        "space_id",
        "status",
        "title",
        "updated_at",
    ],
    "tasks_archived": [
        "archived_at",
        "assignees",
        "created_at",
        "created_by",
        "description",
        "due_date",
        "id",
        "labels",
        "list_id",
        "position",
        "priority",
        "recurrence",
        "recurrence_parent_id",
        "space_id",
        "status",
        "title",
        "updated_at",
    ],
    "tasks_deleted": ["actor_user_id", "created_by", "id", "list_id", "space_id"],
    "timetables": [
        "assignees",
        "color",
        "created_at",
        "created_by",
        "days",
        "defaults",
        "entries",
        "id",
        "name",
        "overrides",
        "schema",
        "tz",
        "updated_at",
        "updated_by",
        "validity",
        "version",
        "week_start",
    ],
}


async def test_record_shapes_are_pinned_to_the_shape_version(db):
    keys = await _covered_record_keys(db)
    assert SYNC_SHAPE_VERSION == 2 and keys == RECORD_KEYS_AT_SHAPE, (
        "A covered §25.6 exporter's record shape changed. Bump "
        "SYNC_SHAPE_VERSION (federation/sync/space/watermark.py) so every "
        "household gets one full stream in the new shape, then update "
        "RECORD_KEYS_AT_SHAPE (and the version asserted here)."
    )

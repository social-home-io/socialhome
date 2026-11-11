"""§25.6 coverage — every space item type heals after being lost.

One table, one shape per row: the host holds a space; a member household
took the host's stream once; then the host changes something the member
never hears live (offline past the outbox, a mesh drop). The host's next
stream must leave the member with the host's state. Both households are
real SQLite databases (every repo from ``app._build_repos``), and the
stream is the real exporters in :data:`RESOURCE_ORDER` fed to the real
:class:`SpaceSyncReceiver` — the only thing skipped is the encryption and
the transport (``receiver._apply`` is the entry each decrypted chunk takes).

Types that are out of the stream by design get a row asserting that
(``docs/protocol/sync.md`` › "Coverage — every space item type").
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from socialhome.app import _build_repos
from socialhome.db.database import AsyncDatabase
from socialhome.domain.calendar import CalendarEvent, CalendarRSVP
from socialhome.domain.conversation import ConversationMessage, SystemChatScope
from socialhome.domain.gallery import GalleryAlbum, GalleryItem
from socialhome.domain.page import Page
from socialhome.domain.post import (
    BazaarBid,
    BazaarListing,
    BazaarMode,
    BazaarStatus,
    Comment,
    CommentType,
    Post,
    PostType,
)
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
    SpaceZone,
)
from socialhome.domain.space_item import stamp_to_db
from socialhome.domain.task import Task, TaskList, TaskStatus
from socialhome.domain.timetable import Timetable
from socialhome.federation.owner_bound_id import (
    GALLERY_ALBUM_KIND,
    GALLERY_ITEM_KIND,
    SPACE_PAGE_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    SPACE_TIMETABLE_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.federation.sync.space.exporter import (
    RESOURCE_ORDER,
    collect_batches,
    record_batches,
)
from socialhome.federation.sync.space.exporters import (
    BansExporter,
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
    MemberPicturesExporter,
    MembersExporter,
    PagesDeletedExporter,
    PagesExporter,
    PollsExporter,
    PostsDeletedExporter,
    PostsExporter,
    SchedulesExporter,
    StickiesDeletedExporter,
    StickiesExporter,
    TaskListsDeletedExporter,
    TaskListsExporter,
    TasksArchivedExporter,
    TasksDeletedExporter,
    TasksExporter,
    TimetablesDeletedExporter,
    TimetablesExporter,
    ZonesDeletedExporter,
    ZonesExporter,
)
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from socialhome.federation.sync.space.window import SyncWindows
from socialhome.infrastructure.event_bus import EventBus

HOST = "host-household"
SPACE = "sp-coverage"
ANNA = "u-anna"  # the host's person (owner)
BOB = "u-bob"  # the member household's person
CARL = "u-carl"  # a third household's person
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def _bound(kind: str) -> str:
    return mint_owner_bound_id(kind, space_id=SPACE, owner_user_id=ANNA)


TIMETABLE = _bound(SPACE_TIMETABLE_KIND)
TASK_LIST = _bound(SPACE_TASK_LIST_KIND)
TASK = _bound(SPACE_TASK_KIND)
PAGE = _bound(SPACE_PAGE_KIND)
ALBUM = _bound(GALLERY_ALBUM_KIND)
ITEM = _bound(GALLERY_ITEM_KIND)


@dataclass
class Home:
    db: AsyncDatabase
    repos: SimpleNamespace
    receiver: SpaceSyncReceiver
    exporters: dict[str, Any]


async def _home(path, local: str) -> Home:
    db = AsyncDatabase(path, batch_timeout_ms=5)
    await db.startup()
    repos = _build_repos(db)
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (local, local, local),
    )
    await repos.space.save(
        Space(
            id=SPACE,
            name="Coverage",
            owner_instance_id=HOST,
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    for uid, role in ((ANNA, SpaceRole.OWNER), (BOB, SpaceRole.MEMBER)):
        await repos.space.save_member(
            SpaceMember(
                space_id=SPACE,
                user_id=uid,
                role=role.value,
                joined_at="2026-01-01T00:00:00+00:00",
            )
        )
    await repos.space.save_member(
        SpaceMember(
            space_id=SPACE,
            user_id=CARL,
            role=SpaceRole.MEMBER.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    windows = SyncWindows(repos.space)
    exporters: dict[str, Any] = {
        "bans": BansExporter(repos.space),
        "members": MembersExporter(repos.space, repos.profile_picture),
        "member_pictures": MemberPicturesExporter(repos.space, repos.profile_picture),
        "bazaar": BazaarExporter(repos.bazaar, windows),
        "posts_deleted": PostsDeletedExporter(repos.space_post),
        "posts": PostsExporter(repos.space_post, windows),
        "comments_deleted": CommentsDeletedExporter(repos.space_post),
        "comments": CommentsExporter(repos.space_post, windows),
        "task_lists": TaskListsExporter(repos.space_task),
        "task_lists_deleted": TaskListsDeletedExporter(repos.space_task),
        "tasks_deleted": TasksDeletedExporter(repos.space_task),
        "tasks": TasksExporter(repos.space_task),
        "tasks_archived": TasksArchivedExporter(repos.space_task),
        "pages_deleted": PagesDeletedExporter(repos.page),
        "pages": PagesExporter(repos.page),
        "gallery_albums_deleted": GalleryAlbumsDeletedExporter(repos.gallery),
        "gallery": GalleryExporter(repos.gallery),
        "gallery_items_deleted": GalleryItemsDeletedExporter(repos.gallery),
        "space_zones_deleted": ZonesDeletedExporter(repos.space_zone),
        "space_zones": ZonesExporter(repos.space_zone),
        "stickies_deleted": StickiesDeletedExporter(repos.sticky),
        "stickies": StickiesExporter(repos.sticky),
        "calendar_deleted": CalendarDeletedExporter(repos.space_cal),
        "calendar": CalendarExporter(repos.space_cal),
        "polls": PollsExporter(repos.space_poll, repos.space_post, windows),
        "schedules": SchedulesExporter(repos.space_poll, repos.space_post, windows),
        "timetables_deleted": TimetablesDeletedExporter(repos.space_timetable),
        "timetables": TimetablesExporter(repos.space_timetable),
    }
    receiver = SpaceSyncReceiver(
        bus=EventBus(),
        encoder=MagicMock(),
        crypto=MagicMock(),
        federation_repo=repos.federation,
        space_repo=repos.space,
        space_post_repo=repos.space_post,
        space_task_repo=repos.space_task,
        page_repo=repos.page,
        sticky_repo=repos.sticky,
        space_calendar_repo=repos.space_cal,
        gallery_repo=repos.gallery,
        zone_repo=repos.space_zone,
        bazaar_repo=repos.bazaar,
        profile_picture_repo=repos.profile_picture,
        poll_repo=repos.space_poll,
        timetable_repo=repos.space_timetable,
        authorship=SpaceAuthorship(
            space_repo=repos.space,
            remote_member_repo=repos.space_remote_member,
            user_repo=repos.user,
        ),
    )
    return Home(db=db, repos=repos, receiver=receiver, exporters=exporters)


@pytest.fixture
async def homes(tmp_dir):
    host = await _home(tmp_dir / "host.db", ANNA)
    member = await _home(tmp_dir / "member.db", BOB)
    yield host, member
    await host.db.shutdown()
    await member.db.shutdown()


async def _stream(host: Home, member: Home) -> None:
    """The host's full stream, resource by resource, into the member."""
    for resource in RESOURCE_ORDER:
        exporter = host.exporters.get(resource)
        if exporter is None:
            continue
        records = await collect_batches(record_batches(exporter, SPACE))
        if records:
            await member.receiver._apply(resource, SPACE, records, provider=HOST)


# ── Item types: seed on the host, lose a change, heal ────────────────────


Step = Callable[[Home], Awaitable[None]]
View = Callable[[Home], Awaitable[Any]]


async def _nothing(_h: Home) -> None:
    return None


async def _post(h: Home, post_id: str = "post-1", **over) -> None:
    fields: dict[str, Any] = {
        "id": post_id,
        "author": ANNA,
        "type": PostType.TEXT,
        "created_at": T0,
        "content": "hello",
    }
    fields.update(over)
    await h.repos.space_post.save(SPACE, Post(**fields))


async def _view_post(h: Home) -> Any:
    got = await h.repos.space_post.get("post-1")
    if got is None:
        return None
    p = got[1]
    return (p.content, p.deleted, {k: sorted(v) for k, v in p.reactions.items()})


async def _edit_post(h: Home) -> None:
    await h.repos.space_post.edit(
        "post-1", "hello, edited", space_id=SPACE, edited_at="2026-11-02 10:00:00"
    )


async def _delete_post(h: Home) -> None:
    await h.repos.space_post.soft_delete("post-1", space_id=SPACE)


def _hours_ago(hours: float) -> str:
    """A reaction stamp relative to now: a removal's stamp is dropped past
    the tombstone horizon, so fixed dates would turn these cases red."""
    return stamp_to_db(datetime.now(UTC) - timedelta(hours=hours))


async def _react(h: Home) -> None:
    await h.repos.space_post.add_reaction(
        "post-1", "🎉", CARL, space_id=SPACE, stamp=_hours_ago(2)
    )


async def _unreact(h: Home) -> None:
    await h.repos.space_post.remove_reaction(
        "post-1", "👍", CARL, space_id=SPACE, stamp=_hours_ago(1)
    )


async def _post_with_reaction(h: Home) -> None:
    await _post(h)
    await h.repos.space_post.add_reaction(
        "post-1", "👍", CARL, space_id=SPACE, stamp=_hours_ago(26)
    )


async def _comment(h: Home) -> None:
    await _post(h)
    await h.repos.space_post.add_comment(
        Comment(
            id="c-1",
            post_id="post-1",
            author=CARL,
            type=CommentType.TEXT,
            created_at=T0,
            content="first",
        ),
        space_id=SPACE,
    )


async def _view_comment(h: Home) -> Any:
    c = await h.repos.space_post.get_comment("c-1")
    return None if c is None else (c.content, c.deleted)


async def _edit_comment(h: Home) -> None:
    await h.repos.space_post.edit_comment(
        "c-1", "first, edited", space_id=SPACE, edited_at="2026-11-02 10:00:00"
    )


async def _delete_comment(h: Home) -> None:
    await h.repos.space_post.soft_delete_comment("c-1", space_id=SPACE)


async def _sticky(h: Home) -> None:
    await h.repos.sticky.add(
        author=ANNA, content="milk", space_id=SPACE, sticky_id="sticky-1"
    )


async def _view_sticky(h: Home) -> Any:
    s = await h.repos.sticky.get("sticky-1")
    deleted = await h.repos.sticky.is_deleted("sticky-1", space_id=SPACE)
    return (None if s is None else s.content, deleted)


async def _delete_sticky(h: Home) -> None:
    await h.repos.sticky.delete("sticky-1", space_id=SPACE)


async def _event(h: Home) -> None:
    await h.repos.space_cal.save_event(
        CalendarEvent(
            id="ev-1",
            calendar_id=SPACE,
            summary="Picnic",
            start=T0 + timedelta(days=3),
            end=T0 + timedelta(days=3, hours=2),
            created_by=ANNA,
            rsvp_enabled=True,
        ),
        space_id=SPACE,
    )


async def _view_event(h: Home) -> Any:
    got = await h.repos.space_cal.get_event("ev-1")
    deleted = await h.repos.space_cal.is_event_deleted("ev-1", space_id=SPACE)
    return (None if got is None else got[1].summary, deleted)


async def _delete_event(h: Home) -> None:
    await h.repos.space_cal.delete_event("ev-1", space_id=SPACE)


_OCC = (T0 + timedelta(days=3)).isoformat()


async def _event_with_rsvps(h: Home) -> None:
    await _event(h)
    for uid, status in ((CARL, "going"), (ANNA, "maybe")):
        await h.repos.space_cal.upsert_rsvp(
            CalendarRSVP(
                event_id="ev-1",
                user_id=uid,
                status=status,
                updated_at="2026-11-01T10:00:00+00:00",
                occurrence_at=_OCC,
            ),
            space_id=SPACE,
        )


async def _view_rsvps(h: Home) -> Any:
    return sorted(
        (r.user_id, r.occurrence_at, r.status)
        for r in await h.repos.space_cal.list_rsvps("ev-1")
    )


async def _change_rsvps(h: Home) -> None:
    # Carl changed his answer, Anna withdrew hers — neither reached Bob.
    await h.repos.space_cal.upsert_rsvp(
        CalendarRSVP(
            event_id="ev-1",
            user_id=CARL,
            status="declined",
            updated_at="2026-11-02T10:00:00+00:00",
            occurrence_at=_OCC,
        ),
        space_id=SPACE,
    )
    await h.repos.space_cal.remove_rsvp(
        "ev-1", ANNA, occurrence_at=_OCC, space_id=SPACE
    )


async def _poll(h: Home) -> None:
    await _post(h, type=PostType.POLL, content="Lunch?")
    await h.repos.space_poll.create_poll(
        post_id="post-1",
        question="Lunch?",
        closes_at=None,
        allow_multiple=False,
        options=[
            {"id": "opt-a", "text": "Pizza", "position": 0},
            {"id": "opt-b", "text": "Soup", "position": 1},
        ],
    )
    await h.repos.space_poll.cast_vote_in_space(
        space_id=SPACE, post_id="post-1", option_id="opt-a", voter_user_id=ANNA
    )


async def _view_poll(h: Home) -> Any:
    meta = await h.repos.space_poll.get_meta("post-1")
    if meta is None:
        return None
    return (
        meta["question"],
        meta["closed"],
        await h.repos.space_poll.list_options_with_counts("post-1"),
        await h.repos.space_poll.list_votes("post-1"),
    )


async def _vote_and_close(h: Home) -> None:
    await h.repos.space_poll.cast_vote_in_space(
        space_id=SPACE, post_id="post-1", option_id="opt-b", voter_user_id=CARL
    )
    await h.repos.space_poll.cast_vote_in_space(
        space_id=SPACE, post_id="post-1", option_id="opt-b", voter_user_id=ANNA
    )
    await h.repos.space_poll.close_in_space("post-1", space_id=SPACE)


async def _schedule(h: Home) -> None:
    await _post(h, type=PostType.SCHEDULE, content="When?")
    await h.repos.space_poll.create_schedule_poll_in_space(
        space_id=SPACE,
        post_id="post-1",
        title="When?",
        deadline=None,
        slots=[
            {"id": "slot-1", "slot_date": "2026-11-20", "position": 0},
            {"id": "slot-2", "slot_date": "2026-11-21", "position": 1},
        ],
    )
    await h.repos.space_poll.upsert_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-1", user_id=CARL, response="maybe"
    )


async def _view_schedule(h: Home) -> Any:
    meta = await h.repos.space_poll.get_schedule_meta("post-1")
    if meta is None:
        return None
    return (
        meta["title"],
        meta["finalized_slot_id"],
        meta["closed"],
        await h.repos.space_poll.list_schedule_slots("post-1"),
        sorted(
            (r["slot_id"], r["user_id"], r["availability"])
            for r in await h.repos.space_poll.list_schedule_responses("post-1")
        ),
    )


async def _answer_and_finalize(h: Home) -> None:
    await h.repos.space_poll.upsert_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-2", user_id=CARL, response="yes"
    )
    await h.repos.space_poll.delete_schedule_response_in_space(
        space_id=SPACE, slot_id="slot-1", user_id=CARL
    )
    await h.repos.space_poll.finalize_schedule_poll_in_space(
        space_id=SPACE, post_id="post-1", slot_id="slot-2"
    )


async def _timetable(h: Home) -> None:
    await h.repos.space_timetable.insert(
        Timetable(
            id=TIMETABLE,
            name="School",
            created_by=ANNA,
            created_at=T0,
            updated_at=T0,
            updated_by=ANNA,
        ),
        space_id=SPACE,
    )


async def _view_timetable(h: Home) -> Any:
    got = await h.repos.space_timetable.get(TIMETABLE)
    return (
        None if got is None else got[1].name,
        await h.repos.space_timetable.is_tombstoned(TIMETABLE),
    )


async def _delete_timetable(h: Home) -> None:
    await h.repos.space_timetable.soft_delete(
        TIMETABLE, space_id=SPACE, at=T0 + timedelta(days=1)
    )


async def _member_role(h: Home) -> None:
    await h.repos.space.save_member(
        SpaceMember(
            space_id=SPACE,
            user_id=CARL,
            role=SpaceRole.MODERATOR.value,
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )


async def _view_roster(h: Home) -> Any:
    carl = await h.repos.space.get_member(SPACE, CARL)
    return (
        None if carl is None else carl.role,
        await h.repos.space.is_banned(SPACE, CARL),
    )


async def _ban(h: Home) -> None:
    await h.repos.space.ban_member(space_id=SPACE, user_id=CARL, banned_by=ANNA)


async def _task_list(h: Home) -> None:
    await h.repos.space_task.save_list(
        TaskList(id=TASK_LIST, name="Chores", created_by=ANNA), space_id=SPACE
    )


async def _task(h: Home) -> None:
    await _task_list(h)
    await h.repos.space_task.save(
        Task(
            id=TASK,
            list_id=TASK_LIST,
            title="Dishes",
            status=TaskStatus.TODO,
            position=0,
            created_by=ANNA,
            created_at=T0,
            updated_at=T0,
        ),
        space_id=SPACE,
    )


async def _view_tasks(h: Home) -> Any:
    lst = await h.repos.space_task.get_list(TASK_LIST)
    task = await h.repos.space_task.get(TASK)
    return (
        None if lst is None else lst[1].name,
        None if task is None else (task[1].title, task[1].status),
        await h.repos.space_task.is_task_deleted(TASK, space_id=SPACE),
    )


async def _task_done(h: Home) -> None:
    got = await h.repos.space_task.get(TASK)
    assert got is not None
    await h.repos.space_task.save(
        replace(got[1], status=TaskStatus.DONE, updated_at=T0 + timedelta(hours=1)),
        space_id=SPACE,
    )


async def _delete_task(h: Home) -> None:
    await h.repos.space_task.delete(TASK, space_id=SPACE)


async def _page(h: Home) -> None:
    await h.repos.page.save(
        Page(
            id=PAGE,
            title="Rules",
            content="Be kind",
            created_by=ANNA,
            created_at=T0.isoformat(),
            updated_at=T0.isoformat(),
            space_id=SPACE,
        ),
        space_id=SPACE,
    )


async def _view_page(h: Home) -> Any:
    page = await h.repos.page.get_space_page(PAGE, space_id=SPACE)
    return (
        None if page is None else page.content,
        await h.repos.page.is_page_deleted(PAGE, space_id=SPACE),
    )


async def _delete_page(h: Home) -> None:
    await h.repos.page.delete(PAGE, space_id=SPACE)


async def _album(h: Home) -> None:
    await h.repos.gallery.create_album_in_space(
        GalleryAlbum(
            id=ALBUM,
            space_id=SPACE,
            owner_user_id=ANNA,
            name="Summer",
            created_at=T0.isoformat(),
        ),
        space_id=SPACE,
    )


async def _item(h: Home) -> None:
    await _album(h)
    await h.repos.gallery.create_item_in_space(
        GalleryItem(
            id=ITEM,
            album_id=ALBUM,
            uploaded_by=ANNA,
            item_type="photo",
            url="/api/media/a.webp",
            thumbnail_url="/api/media/a_t.webp",
            width=10,
            height=10,
            caption="Beach",
            created_at=T0.isoformat(),
        ),
        space_id=SPACE,
    )


async def _view_gallery(h: Home) -> Any:
    album = await h.repos.gallery.get_album(ALBUM)
    item = await h.repos.gallery.get_item(ITEM)
    return (
        None if album is None else album.name,
        None if item is None else item.caption,
    )


async def _delete_item(h: Home) -> None:
    await h.repos.gallery.delete_item_in_space(ITEM, space_id=SPACE)


async def _delete_album(h: Home) -> None:
    await h.repos.gallery.delete_album_in_space(ALBUM, space_id=SPACE)


async def _zone(h: Home) -> None:
    await h.repos.space_zone.upsert(
        SpaceZone(
            id="zone-1",
            space_id=SPACE,
            name="School",
            latitude=52.1234,
            longitude=4.5678,
            radius_m=200,
            created_by=ANNA,
            created_at=T0.isoformat(),
            updated_at=T0.isoformat(),
        ),
        space_id=SPACE,
    )


async def _view_zone(h: Home) -> Any:
    zone = await h.repos.space_zone.get("zone-1")
    return (
        None if zone is None else zone.name,
        await h.repos.space_zone.is_deleted("zone-1", space_id=SPACE),
    )


async def _delete_zone(h: Home) -> None:
    await h.repos.space_zone.delete("zone-1", space_id=SPACE)


async def _listing(h: Home) -> None:
    await _post(h, type=PostType.BAZAAR, content="Bike")
    await h.repos.bazaar.save_listing(
        BazaarListing(
            post_id="post-1",
            space_id=SPACE,
            seller_user_id=ANNA,
            mode=BazaarMode.FIXED,
            title="Bike",
            end_time="2027-01-01T00:00:00+00:00",
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=T0.isoformat(),
            price=50,
        ),
        space_id=SPACE,
    )


async def _view_listing(h: Home) -> Any:
    lst = await h.repos.bazaar.get_listing("post-1")
    return None if lst is None else (lst.title, lst.status, lst.price)


async def _bid(h: Home, bid_id: str, amount: int) -> None:
    await h.repos.bazaar.place_bid(
        BazaarBid(
            id=bid_id,
            listing_post_id="post-1",
            bidder_user_id=CARL,
            amount=amount,
            created_at=f"2026-11-0{amount // 10}T10:00:00+00:00",
            message="mine?",
        ),
        space_id=SPACE,
    )


async def _listing_with_bid(h: Home) -> None:
    await _listing(h)
    await _bid(h, "bid-1", 40)


async def _view_bids(h: Home) -> Any:
    return [
        (b.id, b.amount, b.message, b.rejected, b.withdrawn)
        for b in await h.repos.bazaar.list_bids("post-1")
    ]


async def _more_bids(h: Home) -> None:
    # A new bid, and the first one declined — neither reached Bob.
    await _bid(h, "bid-2", 45)
    await h.repos.bazaar.reject_offer("bid-1", reason="too low")


async def _sell(h: Home) -> None:
    got = await h.repos.bazaar.get_listing("post-1")
    assert got is not None
    await h.repos.bazaar.save_listing(
        replace(got, status=BazaarStatus.SOLD, winner_user_id=CARL, price=45),
        space_id=SPACE,
    )


def _webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color="red").save(buf, format="WEBP")
    return buf.getvalue()


async def _carl_picture(h: Home) -> None:
    await h.repos.profile_picture.set_member_picture(
        SPACE, CARL, bytes_webp=_webp(), hash="0123456789abcdef", width=8, height=8
    )
    await h.repos.space.set_member_profile(SPACE, CARL, picture_hash="0123456789abcdef")


async def _view_picture(h: Home) -> Any:
    return await h.repos.profile_picture.get_member_picture(SPACE, CARL) is not None


@dataclass(frozen=True)
class Case:
    name: str
    seed: Step
    change: Step
    view: View


CASES: tuple[Case, ...] = (
    Case("member role changed", _nothing, _member_role, _view_roster),
    Case("member banned", _nothing, _ban, _view_roster),
    Case("member picture set", _nothing, _carl_picture, _view_picture),
    Case("bazaar listing created", _post, _listing, _view_listing),
    Case("bazaar listing sold", _listing, _sell, _view_listing),
    Case("bazaar bids placed and declined", _listing_with_bid, _more_bids, _view_bids),
    Case("post created", _nothing, _post, _view_post),
    Case("post edited", _post, _edit_post, _view_post),
    Case("post deleted", _post, _delete_post, _view_post),
    Case("reaction added on a held post", _post, _react, _view_post),
    Case("reaction removed on a held post", _post_with_reaction, _unreact, _view_post),
    Case("comment created", _post, _comment, _view_comment),
    Case("comment edited", _comment, _edit_comment, _view_comment),
    Case("comment deleted", _comment, _delete_comment, _view_comment),
    Case("task list and task created", _nothing, _task, _view_tasks),
    Case("task changed", _task, _task_done, _view_tasks),
    Case("task deleted", _task, _delete_task, _view_tasks),
    Case("page created", _nothing, _page, _view_page),
    Case("page deleted", _page, _delete_page, _view_page),
    Case("gallery album and item created", _nothing, _item, _view_gallery),
    Case("gallery item deleted", _item, _delete_item, _view_gallery),
    Case("gallery album deleted", _item, _delete_album, _view_gallery),
    Case("zone created", _nothing, _zone, _view_zone),
    Case("zone deleted", _zone, _delete_zone, _view_zone),
    Case("sticky created", _nothing, _sticky, _view_sticky),
    Case("sticky deleted", _sticky, _delete_sticky, _view_sticky),
    Case("calendar event created", _nothing, _event, _view_event),
    Case("calendar event deleted", _event, _delete_event, _view_event),
    Case("calendar RSVPs changed", _event_with_rsvps, _change_rsvps, _view_rsvps),
    Case("poll created", _post, _poll, _view_poll),
    Case("poll votes and closure", _poll, _vote_and_close, _view_poll),
    Case("schedule poll created", _post, _schedule, _view_schedule),
    Case("schedule answers and pick", _schedule, _answer_and_finalize, _view_schedule),
    Case("timetable created", _nothing, _timetable, _view_timetable),
    Case("timetable deleted", _timetable, _delete_timetable, _view_timetable),
)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_a_lost_change_heals_on_the_hosts_next_stream(homes, case: Case):
    host, member = homes
    await case.seed(host)
    await _stream(host, member)
    assert await case.view(member) == await case.view(host), "seed did not sync"
    before = await case.view(member)
    await case.change(host)
    expected = await case.view(host)
    assert expected != before, "the change must change something"
    await _stream(host, member)
    assert await case.view(member) == expected


class _ChatSink:
    """Records what the receiver hands the space chat's inbound handlers —
    which run each record through the live create / delete rule (their
    own tests: ``tests/services/federation_inbound/test_space_chat.py``)."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.deletions: list[str] = []

    async def apply_sync_records(self, space_id, records, *, provider):
        self.messages += [str(r.get("message_id") or r.get("id")) for r in records]
        return 0

    async def apply_sync_tombstones(self, space_id, records, *, provider):
        self.deletions += [str(r.get("message_id") or r.get("id")) for r in records]


async def test_a_lost_chat_message_and_its_delete_reach_the_chat_sink(homes):
    host, member = homes
    sink = _ChatSink()
    member.receiver.attach_chat_sink(sink)
    host.exporters["chat_messages"] = ChatMessagesExporter(
        host.repos.conversation, host.repos.space
    )
    host.exporters["chat_messages_deleted"] = ChatMessagesDeletedExporter(
        host.repos.conversation, host.repos.space
    )
    chat = await host.repos.conversation.create_system_chat(
        SystemChatScope.SPACE, space_id=SPACE
    )
    for mid in ("m-live", "m-gone"):
        await host.repos.conversation.save_message(
            ConversationMessage(
                id=mid,
                conversation_id=chat.id,
                sender_user_id=ANNA,
                content="hi",
                created_at=datetime.now(UTC),
            )
        )
    await host.repos.conversation.soft_delete_message("m-gone")
    await _stream(host, member)
    assert sink.messages == ["m-live"]
    assert sink.deletions == ["m-gone"]


# ── Out of the stream by design ──────────────────────────────────────────


def test_the_moderation_queue_never_streams():
    """Pending posts wait on the host for a moderator: a member household
    never holds them, so no resource carries them."""
    assert not [r for r in RESOURCE_ORDER if "moderation" in r or "pending" in r]


async def test_bazaar_offers_threads_and_decline_reasons_never_stream(homes):
    """An offer, its answer and an Ask-the-seller thread are between the
    seller's and the buyer's households only (docs/protocol/bazaar.md), and
    a declined bid's reason is the bidder's: none rides the space stream,
    and a member household never stores a reason from it."""
    host, member = homes
    await _listing_with_bid(host)
    await host.repos.bazaar.reject_offer("bid-1", reason="too low")
    (record,) = await collect_batches(record_batches(host.exporters["bazaar"], SPACE))
    assert not {"offers", "threads", "messages"} & set(record)
    assert record["bids"] and "too low" not in repr(record)
    await _stream(host, member)
    (bid,) = await member.repos.bazaar.list_bids("post-1")
    assert bid.rejected and bid.rejection_reason is None


def test_space_chat_reactions_are_not_streamed():
    """Chat reactions carry no order stamp, so a snapshot could only revert
    a newer relayed one: no resource carries them (a missed chat reaction
    is not healed — docs/protocol/sync.md, "Coverage")."""
    assert not [r for r in RESOURCE_ORDER if "reaction" in r]

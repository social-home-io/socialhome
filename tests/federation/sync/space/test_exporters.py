"""Unit tests for each resource exporter.

These exercise the record-serialisation paths — covering enum → str,
datetime → ISO, frozenset → list, nested dataclass → dict conversions
so the receiver has JSON-serialisable input.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace


from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.page import Page
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.space import SpaceMember
from socialhome.domain.sticky import Sticky
from socialhome.domain.task import RecurrenceRule, Task, TaskPriority, TaskStatus
from socialhome.federation.sync.space.window import SyncWindows
from socialhome.domain.conversation import ConversationMessage, SystemChatScope
from socialhome.federation.sync.space.exporter import (
    IncrementalExporter,
    collect_batches,
    record_batches,
)
from socialhome.federation.sync.space.exporters import (
    BazaarExporter,
    CalendarDeletedExporter,
    CalendarExporter,
    ChatMessagesDeletedExporter,
    ChatMessagesExporter,
    CommentsDeletedExporter,
    CommentsExporter,
    GalleryExporter,
    PollsExporter,
    PostsDeletedExporter,
    PostsExporter,
    SchedulesExporter,
    BansExporter,
    MembersExporter,
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
    ZonesExporter,
)
from socialhome.domain.task import TaskList
from socialhome.domain.timetable import Timetable
from socialhome.repositories.page_repo import SqlitePageRepo
from socialhome.repositories.space_zone_repo import SqliteSpaceZoneRepo
from socialhome.repositories.task_repo import SqliteSpaceTaskRepo
from socialhome.repositories.timetable_repo import SqliteSpaceTimetableRepo
from socialhome.repositories.bazaar_repo import SqliteBazaarRepo
from socialhome.repositories.calendar_repo import SqliteSpaceCalendarRepo
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.space_poll_repo import SqliteSpacePollRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.sticky_repo import SqliteStickyRepo


class _NoSpaces:
    async def get(self, space_id):
        return None


#: A space with no retention known: everything streams (keep forever).
_WINDOWS = SyncWindows(_NoSpaces())  # type: ignore[arg-type]


class _FakeSpacePostRepo:
    def __init__(self, posts, comments_by_post=None, anchors=None):
        self._posts = posts
        #: ``hidden_from_feed`` anchor posts: in ``list_sync_page``, never
        #: in ``list_feed`` — the same split the SQLite repo makes.
        self._anchors = anchors or []
        self._comments_by_post = comments_by_post or {}

    async def list_feed(self, space_id, *, before=None, limit=20):
        return self._posts

    async def list_sync_page(
        self,
        space_id,
        *,
        deleted=False,
        cutoff=None,
        exempt_types=(),
        cursor=None,
        limit=200,
        since=None,
    ):
        return ([] if deleted else self._posts + self._anchors), None

    async def list_comments_sync_page(
        self,
        space_id,
        *,
        deleted=False,
        cutoff=None,
        exempt_types=(),
        cursor=None,
        limit=200,
        since=None,
    ):
        rows = [c for cs in self._comments_by_post.values() for c in cs]
        return [c for c in rows if c.deleted == deleted], None


class _FakeSpaceRepo:
    def __init__(self, members=None, bans=None):
        self._members = members or []
        self._bans = bans or []

    async def list_members(self, space_id):
        return self._members

    async def list_bans(self, space_id):
        return self._bans


async def test_members_exporter():
    from socialhome.federation.sync.space.exporters import MembersExporter

    member = SpaceMember(
        space_id="sp-1",
        user_id="u-1",
        role="member",
        joined_at="2026-04-18T00:00:00+00:00",
    )
    ex = MembersExporter(_FakeSpaceRepo(members=[member]))
    recs = await ex.list_records("sp-1")
    assert recs[0]["user_id"] == "u-1"
    assert recs[0]["role"] == "member"


async def test_pre_moderator_members_exporter_downgrades_only_moderators():
    from socialhome.federation.sync.space.exporters import MembersExporter
    from socialhome.federation.sync.space.exporters.members import (
        PreModeratorMembersExporter,
    )

    rows = [
        SpaceMember(space_id="sp-1", user_id=uid, role=role, joined_at="2026")
        for uid, role in (
            ("u-o", "owner"),
            ("u-a", "admin"),
            ("u-mod", "moderator"),
            ("u-m", "member"),
        )
    ]
    ex = PreModeratorMembersExporter(MembersExporter(_FakeSpaceRepo(members=rows)))
    assert ex.resource == "members"
    recs = await ex.list_records("sp-1")
    assert {r["user_id"]: r["role"] for r in recs} == {
        "u-o": "owner",
        "u-a": "admin",
        "u-mod": "member",
        "u-m": "member",
    }


async def test_bans_exporter():
    from socialhome.federation.sync.space.exporters import BansExporter

    ex = BansExporter(
        _FakeSpaceRepo(
            bans=[
                {"user_id": "u-x", "banned_by": "admin", "reason": "spam"},
            ]
        )
    )
    assert (await ex.list_records("sp-1"))[0]["user_id"] == "u-x"


async def test_posts_exporter_serialises_enums_and_datetimes():
    from socialhome.federation.sync.space.exporters import PostsExporter

    post = Post(
        id="p-1",
        author="u-1",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
        content="hi",
    )
    ex = PostsExporter(_FakeSpacePostRepo([post]), _WINDOWS)
    recs = await ex.list_records("sp-1")
    assert recs[0]["type"] == "text"  # enum → str
    assert recs[0]["created_at"].startswith("2026-04-18")  # datetime → ISO
    assert recs[0]["reactions"] == {}  # frozenset → sorted list


async def test_posts_exporter_ships_hidden_anchor_posts_with_their_flag():
    """A bazaar / calendar anchor the author did not announce is exported.

    ``bazaar_listings.post_id`` references ``space_posts(id)``; a joiner
    that never receives the anchor cannot store the listing. The exporter
    therefore enumerates through ``list_sync_page`` (anchors included) and
    ships ``hidden_from_feed`` so the joiner's feed stays as clean as the
    provider's.
    """
    from socialhome.federation.sync.space.exporters import PostsExporter

    when = datetime(2026, 9, 19, tzinfo=timezone.utc)
    shown = Post(
        id="p-shown", author="u-1", type=PostType.TEXT, created_at=when, content="hi"
    )
    anchor = Post(
        id="p-anchor",
        author="u-1",
        type=PostType.TEXT,
        created_at=when,
        content="listing card",
        hidden_from_feed=True,
    )
    ex = PostsExporter(_FakeSpacePostRepo([shown], anchors=[anchor]), _WINDOWS)
    recs = {r["id"]: r for r in await ex.list_records("sp-1")}
    assert set(recs) == {"p-shown", "p-anchor"}
    assert recs["p-anchor"]["hidden_from_feed"] is True
    assert recs["p-shown"]["hidden_from_feed"] is False


async def test_comments_exporter_walks_posts():
    from socialhome.federation.sync.space.exporters import CommentsExporter

    post = Post(
        id="p-1",
        author="u-1",
        type=PostType.TEXT,
        created_at=datetime.now(timezone.utc),
    )
    comment = Comment(
        id="c-1",
        post_id="p-1",
        author="u-2",
        type=CommentType.TEXT,
        created_at=datetime.now(timezone.utc),
        content="nice",
    )
    repo = _FakeSpacePostRepo([post], {"p-1": [comment]})
    ex = CommentsExporter(repo, _WINDOWS)
    recs = await ex.list_records("sp-1")
    assert len(recs) == 1
    assert recs[0]["id"] == "c-1"
    assert recs[0]["type"] == "text"


async def test_tasks_exporter_normalises_status_and_assignees():
    from socialhome.federation.sync.space.exporters import TasksExporter

    now = datetime.now(timezone.utc)
    task = Task(
        id="t-1",
        list_id="list-1",
        title="X",
        status=TaskStatus.TODO,
        position=0,
        created_by="u-1",
        created_at=now,
        updated_at=now,
        assignees=("u-2", "u-3"),
        due_date=date(2026, 4, 30),
        recurrence=RecurrenceRule(rrule="FREQ=DAILY"),
    )

    class _Repo:
        async def list_by_space(self, space_id, *, since_seq=None):
            return [task]

    recs = await TasksExporter(_Repo()).list_records("sp-1")
    assert recs[0]["status"] == "todo"
    assert recs[0]["assignees"] == ["u-2", "u-3"]
    assert recs[0]["due_date"] == "2026-04-30"
    assert isinstance(recs[0]["recurrence"], dict)
    assert recs[0]["recurrence"]["rrule"] == "FREQ=DAILY"
    # v_40: the shared wire codec — priority key always present.
    assert recs[0]["priority"] is None
    assert recs[0]["labels"] == []
    assert recs[0]["space_id"] == "sp-1"


async def test_tasks_exporters_carry_priority_labels_and_archived_at():
    from socialhome.federation.sync.space.exporters import TasksArchivedExporter

    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    task = Task(
        id="t-9",
        list_id="l",
        title="Archived",
        status=TaskStatus.DONE,
        position=0,
        created_by="u-1",
        created_at=now,
        updated_at=now,
        archived_at=now,
        priority=TaskPriority.URGENT,
        labels=("Bills",),
    )

    class _Repo:
        async def list_by_space(self, space_id, *, since_seq=None):
            return [task]

    recs = await TasksArchivedExporter(_Repo()).list_records("sp-1")
    assert recs[0]["priority"] == "urgent"
    assert recs[0]["labels"] == ["Bills"]
    assert recs[0]["archived_at"] == now.isoformat()


async def test_tasks_archived_filters_to_archived_at():
    """TasksArchivedExporter surfaces tasks with archived_at set,
    regardless of their status (a DONE task is *not* archived unless
    the user explicitly archived it).
    """
    from socialhome.federation.sync.space.exporters import (
        TasksArchivedExporter,
        TasksExporter,
    )

    now = datetime.now(timezone.utc)
    active = Task(
        id="t-1",
        list_id="l",
        title="Active",
        status=TaskStatus.TODO,
        position=0,
        created_by="u-1",
        created_at=now,
        updated_at=now,
    )
    done_not_archived = Task(
        id="t-2",
        list_id="l",
        title="Done but not archived",
        status=TaskStatus.DONE,
        position=1,
        created_by="u-1",
        created_at=now,
        updated_at=now,
    )
    archived = Task(
        id="t-3",
        list_id="l",
        title="Archived",
        status=TaskStatus.TODO,
        position=2,
        created_by="u-1",
        created_at=now,
        updated_at=now,
        archived_at=now,
    )

    class _Repo:
        async def list_by_space(self, space_id, *, since_seq=None):
            return [active, done_not_archived, archived]

    archived_recs = await TasksArchivedExporter(_Repo()).list_records("sp-1")
    assert {r["id"] for r in archived_recs} == {"t-3"}

    active_recs = await TasksExporter(_Repo()).list_records("sp-1")
    assert {r["id"] for r in active_recs} == {"t-1", "t-2"}


async def test_pages_exporter():
    from socialhome.federation.sync.space.exporters import PagesExporter

    page = Page(
        id="pg-1",
        title="Welcome",
        content="Hello",
        created_by="u-1",
        created_at="2026-04-18T00:00:00+00:00",
        updated_at="2026-04-18T00:00:00+00:00",
        space_id="sp-1",
    )

    class _Repo:
        async def list(self, *, space_id, since_seq=None):
            return [page]

        async def list_conflict_sides(self, page_id, *, space_id):
            return []

    recs = await PagesExporter(_Repo()).list_records("sp-1")
    assert recs[0]["id"] == "pg-1"
    assert recs[0]["seq"] == 0 and recs[0]["conflict"] == []


async def test_stickies_exporter():
    from socialhome.federation.sync.space.exporters import StickiesExporter

    sticky = Sticky(
        id="s-1",
        author="u-1",
        content="note",
        color="yellow",
        position_x=1.0,
        position_y=2.0,
        created_at="2026-04-18T00:00:00+00:00",
        updated_at="2026-04-18T00:00:00+00:00",
        space_id="sp-1",
    )

    class _Repo:
        async def list(self, *, space_id, since_seq=None):
            return [sticky]

    recs = await StickiesExporter(_Repo()).list_records("sp-1")
    assert recs[0]["id"] == "s-1"


async def test_calendar_exporter_serialises_datetimes():
    from socialhome.federation.sync.space.exporters import CalendarExporter

    event = CalendarEvent(
        id="e-1",
        calendar_id="cal-1",
        summary="Sync meeting",
        start=datetime(2026, 4, 18, 10, tzinfo=timezone.utc),
        end=datetime(2026, 4, 18, 11, tzinfo=timezone.utc),
        created_by="u-1",
    )

    class _Repo:
        async def list_events_in_range(self, space_id, *, start, end, since=None):
            return [event]

    recs = await CalendarExporter(_Repo()).list_records("sp-1")
    assert recs[0]["id"] == "e-1"
    assert recs[0]["start"].startswith("2026-04-18")


async def test_gallery_exporter_emits_albums_then_items():
    from socialhome.domain.gallery import GalleryAlbum, GalleryItem
    from socialhome.federation.sync.space.exporters import GalleryExporter

    album = GalleryAlbum(
        id="a-1",
        space_id="sp-1",
        owner_user_id="u-1",
        name="Trip",
    )
    item = GalleryItem(
        id="i-1",
        album_id="a-1",
        uploaded_by="u-1",
        item_type="photo",
        url="/m/x.jpg",
        thumbnail_url="/m/x-thumb.jpg",
        width=1024,
        height=768,
    )

    class _Repo:
        async def list_albums_sync_page(
            self, space_id, *, cursor=None, limit=200, since=None
        ):
            return [album], None

        async def list_items_sync_page(
            self, space_id, *, cursor=None, limit=200, since=None
        ):
            # No retention window: nothing prunes gallery items, so a joiner
            # gets every photo the host still shows.
            return [item], None

    recs = await GalleryExporter(_Repo()).list_records("sp-1")
    assert len(recs) == 2
    assert recs[0]["kind"] == "album"
    assert recs[1]["kind"] == "item"


async def test_polls_exporter_walks_posts_with_polls():
    from socialhome.federation.sync.space.exporters import PollsExporter

    post = SimpleNamespace(id="p-1")

    class _Posts:
        async def list_sync_page(self, space_id, **_kw):
            return [post], None

    class _Polls:
        async def get_meta(self, post_id):
            return {"question": "Pizza?"}

        async def list_options_with_counts(self, post_id):
            return [{"id": "opt-a", "text": "Yes", "count": 3}]

    recs = await PollsExporter(_Polls(), _Posts(), _WINDOWS).list_records("sp-1")
    assert recs[0]["post_id"] == "p-1"
    assert recs[0]["meta"]["question"] == "Pizza?"
    assert recs[0]["options"][0]["id"] == "opt-a"


async def test_polls_exporter_skips_posts_without_polls():
    from socialhome.federation.sync.space.exporters import PollsExporter

    post = SimpleNamespace(id="p-2")

    class _Posts:
        async def list_sync_page(self, space_id, **_kw):
            return [post], None

    class _Polls:
        async def get_meta(self, post_id):
            return None

        async def list_options_with_counts(self, post_id):
            return []

    recs = await PollsExporter(_Polls(), _Posts(), _WINDOWS).list_records("sp-1")
    assert recs == []


async def test_zones_exporter_serialises_catalogue():
    """Per-space zones (§23.8.7) ride the chunked sync so a remote
    member instance joining mid-life picks up every zone, not only
    the ones added after the join. The exporter is a thin asdict
    over what the repo returns."""
    from socialhome.domain.space import SpaceZone
    from socialhome.federation.sync.space.exporters import ZonesExporter

    z = SpaceZone(
        id="z_office",
        space_id="sp-1",
        name="Office",
        latitude=47.3769,
        longitude=8.5417,
        radius_m=150,
        color="#3b82f6",
        created_by="u-1",
        created_at="2026-04-27T00:00:00+00:00",
        updated_at="2026-04-27T00:00:00+00:00",
    )

    class _Repo:
        async def list_for_space(self, space_id, *, since_seq=None):
            return [z]

    recs = await ZonesExporter(_Repo()).list_records("sp-1")
    assert len(recs) == 1
    assert recs[0]["id"] == "z_office"
    assert recs[0]["name"] == "Office"
    assert recs[0]["latitude"] == 47.3769
    assert recs[0]["radius_m"] == 150
    # Match the on-the-wire shape the receiver expects.
    assert recs[0]["color"] == "#3b82f6"


async def test_bazaar_exporter_serialises_listings():
    """F4: bazaar listings ship under the ``bazaar`` resource so a
    catch-up sync rebuilds full listing cards (mode / price / photos /
    status) on the receiver — not just the wrapper post's caption."""
    from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
    from socialhome.federation.sync.space.exporters import BazaarExporter

    listing = BazaarListing(
        post_id="bzr-1",
        space_id="sp-1",
        seller_user_id="u-seller",
        mode=BazaarMode.FIXED,
        title="Vintage chair",
        end_time="2026-06-01T00:00:00+00:00",
        currency="USD",
        status=BazaarStatus.ACTIVE,
        created_at="2026-05-23T10:00:00+00:00",
        description="A nice chair",
        image_urls=("api/media/chair-1.webp", "api/media/chair-2.webp"),
        price=4500,
    )

    class _Repo:
        async def list_sync_page(self, space_id, **_kw):
            return [listing], None

    recs = await BazaarExporter(_Repo(), _WINDOWS).list_records("sp-1")
    assert len(recs) == 1
    r = recs[0]
    assert r["post_id"] == "bzr-1"
    assert r["mode"] == "fixed"  # enum value, not enum instance
    assert r["status"] == "active"
    assert r["price"] == 4500
    assert r["image_urls"] == [
        "api/media/chair-1.webp",
        "api/media/chair-2.webp",
    ]
    # Auction-specific fields ship as-is (None for non-auction listings).
    assert r["start_price"] is None
    assert r["step_price"] is None


async def test_member_pictures_exporter_inlines_webp_bytes():
    """F6: per-space avatars catch up so a new joiner sees existing
    members' faces instead of broken <img>."""
    from socialhome.domain.space import SpaceMember
    from socialhome.federation.sync.space.exporters import MemberPicturesExporter

    members = [
        SpaceMember(
            space_id="sp-1",
            user_id="u-alice",
            role="member",
            joined_at="2026-01-01",
            picture_hash="abc123",
        ),
        SpaceMember(  # no picture — skipped
            space_id="sp-1",
            user_id="u-bob",
            role="member",
            joined_at="2026-01-01",
        ),
    ]

    class _SpaceRepo:
        async def list_members(self, space_id):
            return members

    class _PicRepo:
        async def get_member_picture(self, space_id, user_id):
            if user_id == "u-alice":
                return (b"\x89WEBP-bytes", "abc123")
            return None

    recs = await MemberPicturesExporter(_SpaceRepo(), _PicRepo()).list_records(
        "sp-1",
    )
    # Only the one with a picture made it.
    assert len(recs) == 1
    r = recs[0]
    assert r["user_id"] == "u-alice"
    assert r["picture_hash"] == "abc123"
    # base64 round-trips.
    import base64

    assert base64.b64decode(r["picture_webp_base64"]) == b"\x89WEBP-bytes"


async def test_member_pictures_exporter_skips_when_picture_missing():
    """Member row has hash but the bytes vanished (race / dropped row)
    — skip rather than ship a hash with no bytes."""
    from socialhome.domain.space import SpaceMember
    from socialhome.federation.sync.space.exporters import MemberPicturesExporter

    class _SpaceRepo:
        async def list_members(self, space_id):
            return [
                SpaceMember(
                    space_id="sp-1",
                    user_id="u-alice",
                    role="member",
                    joined_at="2026-01-01",
                    picture_hash="abc",
                ),
            ]

    class _PicRepo:
        async def get_member_picture(self, space_id, user_id):
            return None  # bytes gone

    recs = await MemberPicturesExporter(_SpaceRepo(), _PicRepo()).list_records(
        "sp-1",
    )
    assert recs == []


async def test_schedules_exporter_emits_slot_defs():
    """F5: schedule polls catch up via the ``schedules`` resource so a
    remote member's slot picker isn't empty after §25.6 sync."""
    from datetime import datetime, timezone
    from socialhome.domain.post import Post, PostType
    from socialhome.federation.sync.space.exporters import SchedulesExporter

    post = Post(
        id="p-sched",
        author="u",
        type=PostType.SCHEDULE,
        created_at=datetime.now(timezone.utc),
    )

    class _PostRepo:
        async def list_sync_page(self, space_id, **_kw):
            return [post], None

    class _PollRepo:
        async def get_schedule_meta(self, post_id):
            if post_id == "p-sched":
                return {"title": "Picnic?", "deadline": None}
            return None

        async def list_schedule_slots(self, post_id):
            return [
                {
                    "id": "s1",
                    "slot_date": "2026-07-01",
                    "start_time": "14:00",
                    "end_time": "16:00",
                    "position": 0,
                },
                {
                    "id": "s2",
                    "slot_date": "2026-07-02",
                    "start_time": None,
                    "end_time": None,
                    "position": 1,
                },
            ]

    recs = await SchedulesExporter(_PollRepo(), _PostRepo(), _WINDOWS).list_records(
        "sp-1"
    )
    assert len(recs) == 1
    r = recs[0]
    assert r["post_id"] == "p-sched"
    assert r["title"] == "Picnic?"
    assert len(r["slots"]) == 2
    assert r["slots"][0]["id"] == "s1"
    assert r["slots"][1]["start_time"] is None


# ── §25.6 incremental sessions (migration 0086 change stamps) ────────────


async def _seq(db) -> int:
    row = await db.fetchone("SELECT seq FROM sync_seq_counter WHERE id=1")
    return int(row["seq"])


async def test_covered_exporters_stream_only_rows_changed_since_a_stamp(db):
    """Every covered resource's ``iter_changed`` streams exactly the rows
    stamped above ``since``; ``record_batches`` without ``since`` (or for a
    resource kept full) streams everything, as before."""

    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    posts = SqliteSpacePostRepo(db)
    polls = SqliteSpacePollRepo(db)
    windows = SyncWindows(SqliteSpaceRepo(db))
    for pid in ("p-quiet", "p-edit", "p-vote", "p-gone"):
        await posts.save(
            "sp",
            Post(
                id=pid,
                author="u",
                type=PostType.TEXT,
                created_at=datetime.now(timezone.utc),
                content=pid,
            ),
        )
    await polls.create_poll(
        post_id="p-vote",
        question="Q?",
        closes_at=None,
        allow_multiple=False,
        options=[{"id": "o-1", "text": "A"}],
    )
    for cid, pid in (("k-quiet", "p-quiet"), ("k-gone", "p-quiet")):
        await posts.add_comment(
            Comment(
                id=cid,
                post_id=pid,
                author="u",
                type=CommentType.TEXT,
                created_at=datetime.now(timezone.utc),
                content=cid,
            ),
            space_id="sp",
        )
    gallery = SqliteGalleryRepo(db)
    cal = SqliteSpaceCalendarRepo(db)
    stickies = SqliteStickyRepo(db)
    sticky = await stickies.add(author="u", content="note", space_id="sp")
    await db.enqueue(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
        " end_dt, created_by) VALUES('ev','sp','Party','2026-07-01T10:00:00',"
        " '2026-07-01T11:00:00','u')"
    )
    mark = await _seq(db)

    await posts.edit("p-edit", "edited", space_id="sp")
    assert await polls.cast_vote_in_space(
        space_id="sp", post_id="p-vote", option_id="o-1", voter_user_id="u2"
    )
    await posts.soft_delete("p-gone", space_id="sp")
    await posts.soft_delete_comment("k-gone", space_id="sp")
    await stickies.delete(sticky.id, space_id="sp", deleted_by="u")

    async def changed(exporter) -> list[dict]:
        assert isinstance(exporter, IncrementalExporter), exporter
        return await collect_batches(record_batches(exporter, "sp", since=mark))

    assert {r["id"] for r in await changed(PostsExporter(posts, windows))} == {
        "p-edit",
        "p-vote",
    }
    assert [r["id"] for r in await changed(PostsDeletedExporter(posts))] == ["p-gone"]
    assert [
        r["post_id"] for r in await changed(PollsExporter(polls, posts, windows))
    ] == ["p-vote"]
    assert await changed(SchedulesExporter(polls, posts, windows)) == []
    assert await changed(CommentsExporter(posts, windows)) == []
    assert [r["id"] for r in await changed(CommentsDeletedExporter(posts))] == [
        "k-gone"
    ]
    assert await changed(GalleryExporter(gallery)) == []
    assert await changed(BazaarExporter(SqliteBazaarRepo(db), windows)) == []
    assert await changed(CalendarExporter(cal)) == []
    assert await changed(CalendarDeletedExporter(cal)) == []
    assert [r["id"] for r in await changed(StickiesDeletedExporter(stickies))] == [
        sticky.id
    ]
    # The live stickies are covered too (migration 0088 follow-up): the
    # deleted one left the board, nothing else changed.
    assert await changed(StickiesExporter(stickies)) == []
    # ``since=None`` is the full stream.
    full = await collect_batches(
        record_batches(PostsExporter(posts, windows), "sp", since=None)
    )
    assert {r["id"] for r in full} == {"p-quiet", "p-edit", "p-vote"}
    # The calendar's changed events, once one changes.
    await db.enqueue("UPDATE space_calendar_events SET summary='Bash' WHERE id='ev'")
    assert [r["id"] for r in await changed(CalendarExporter(cal))] == ["ev"]


async def test_chat_exporters_stream_only_changed_messages(db):

    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    spaces = SqliteSpaceRepo(db)
    convos = SqliteConversationRepo(db)
    chat = await convos.create_system_chat(SystemChatScope.SPACE, space_id="sp")
    for i in range(3):
        await convos.save_message(
            ConversationMessage(
                id=f"m{i}",
                conversation_id=chat.id,
                sender_user_id="u",
                content=f"hi {i}",
                created_at=datetime(2026, 1, 1, 0, i, tzinfo=timezone.utc),
            )
        )
    mark = await _seq(db)
    await convos.soft_delete_message("m1")
    messages = ChatMessagesExporter(convos, spaces)
    deletions = ChatMessagesDeletedExporter(convos, spaces)
    live = await collect_batches(record_batches(messages, "sp", since=mark))
    gone = await collect_batches(record_batches(deletions, "sp", since=mark))
    assert live == [] and [r["id"] for r in gone] == ["m1"]


# ── §25.6 incremental: the productivity resources (migration 0088) ───────


async def test_productivity_exporters_stream_only_rows_changed_since_a_stamp(db):
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    tasks = SqliteSpaceTaskRepo(db)
    pages = SqlitePageRepo(db)
    timetables = SqliteSpaceTimetableRepo(db)
    stickies = SqliteStickyRepo(db)
    zones = SqliteSpaceZoneRepo(db)
    now = datetime.now(timezone.utc)
    for lid in ("l-quiet", "l-rename", "l-gone"):
        await tasks.save_list(TaskList(id=lid, name=lid, created_by="u"), space_id="sp")
    for tid in ("t-quiet", "t-edit", "t-archive", "t-gone"):
        await tasks.save(
            Task(
                id=tid,
                list_id="l-quiet",
                title=tid,
                status=TaskStatus.TODO,
                position=0,
                created_by="u",
                created_at=now,
                updated_at=now,
            ),
            space_id="sp",
        )
    for pid in ("pg-quiet", "pg-edit", "pg-gone"):
        await pages.save(
            Page(
                id=pid,
                title=pid,
                content="body",
                created_by="u",
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
                space_id="sp",
            ),
            space_id="sp",
        )
    for ttid in ("tt-quiet", "tt-edit"):
        await timetables.insert(
            Timetable(
                id=ttid, name=ttid, created_by="u", created_at=now, updated_at=now
            ),
            space_id="sp",
        )
    quiet_sticky = await stickies.add(author="u", content="q", space_id="sp")
    moved = await stickies.add(author="u", content="m", space_id="sp")
    mark = await _seq(db)

    await tasks.save_list(
        TaskList(id="l-rename", name="new", created_by="u"), space_id="sp"
    )
    await tasks.delete_list("l-gone", space_id="sp")
    edited = (await tasks.get("t-edit"))[1]
    await tasks.save(replace(edited, title="edited"), space_id="sp")
    archived = (await tasks.get("t-archive"))[1]
    await tasks.save(replace(archived, archived_at=now), space_id="sp")
    await tasks.delete("t-gone", space_id="sp")
    page = await pages.get_space_page("pg-edit", space_id="sp")
    await pages.save(replace(page, content="edited"), space_id="sp")
    await pages.delete("pg-gone", space_id="sp")
    tt = (await timetables.get("tt-edit"))[1]
    assert await timetables.save(
        replace(tt, name="edited", version=2), space_id="sp", expected_version=1
    )
    await stickies.update_position(moved.id, 5.0, 6.0, space_id="sp")

    async def changed(exporter, key: str = "id") -> list[str]:
        assert isinstance(exporter, IncrementalExporter), exporter
        recs = await collect_batches(record_batches(exporter, "sp", since=mark))
        return sorted(r[key] for r in recs)

    assert await changed(TaskListsExporter(tasks)) == ["l-rename"]
    assert await changed(TaskListsDeletedExporter(tasks)) == ["l-gone"]
    assert await changed(TasksExporter(tasks)) == ["t-edit"]
    assert await changed(TasksArchivedExporter(tasks)) == ["t-archive"]
    assert await changed(TasksDeletedExporter(tasks)) == ["t-gone"]
    assert await changed(PagesExporter(pages)) == ["pg-edit"]
    assert await changed(PagesDeletedExporter(pages)) == ["pg-gone"]
    assert await changed(TimetablesExporter(timetables)) == ["tt-edit"]
    assert await changed(StickiesExporter(stickies)) == [moved.id]
    assert await changed(ZonesExporter(zones)) == []
    # The full stream still carries the untouched rows.
    full = await TasksExporter(tasks).list_records("sp")
    assert {"t-quiet", "t-edit"} <= {r["id"] for r in full}
    assert quiet_sticky.id in {
        r["id"] for r in await StickiesExporter(stickies).list_records("sp")
    }


def test_the_roster_stays_full():
    """Admission depends on the whole roster: never incremental."""
    for exporter in (MembersExporter(None), BansExporter(None)):
        assert not isinstance(exporter, IncrementalExporter)

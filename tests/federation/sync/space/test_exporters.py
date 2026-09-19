"""Unit tests for each resource exporter.

These exercise the record-serialisation paths — covering enum → str,
datetime → ISO, frozenset → list, nested dataclass → dict conversions
so the receiver has JSON-serialisable input.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace


from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.page import Page
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.space import SpaceMember
from socialhome.domain.sticky import Sticky
from socialhome.domain.task import RecurrenceRule, Task, TaskStatus


class _FakeSpacePostRepo:
    def __init__(self, posts, comments_by_post=None, anchors=None):
        self._posts = posts
        #: ``hidden_from_feed`` anchor posts: in ``list_for_sync``, never
        #: in ``list_feed`` — the same split the SQLite repo makes.
        self._anchors = anchors or []
        self._comments_by_post = comments_by_post or {}

    async def list_feed(self, space_id, *, before=None, limit=20):
        return self._posts

    async def list_for_sync(self, space_id, *, limit=1000):
        return self._posts + self._anchors

    async def list_comments(self, post_id):
        return self._comments_by_post.get(post_id, [])


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
    ex = PostsExporter(_FakeSpacePostRepo([post]))
    recs = await ex.list_records("sp-1")
    assert recs[0]["type"] == "text"  # enum → str
    assert recs[0]["created_at"].startswith("2026-04-18")  # datetime → ISO
    assert recs[0]["reactions"] == {}  # frozenset → sorted list


async def test_posts_exporter_ships_hidden_anchor_posts_with_their_flag():
    """A bazaar / calendar anchor the author did not announce is exported.

    ``bazaar_listings.post_id`` references ``space_posts(id)``; a joiner
    that never receives the anchor cannot store the listing. The exporter
    therefore enumerates through ``list_for_sync`` (anchors included) and
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
    ex = PostsExporter(_FakeSpacePostRepo([shown], anchors=[anchor]))
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
    ex = CommentsExporter(repo)
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
        async def list_by_space(self, space_id):
            return [task]

    recs = await TasksExporter(_Repo()).list_records("sp-1")
    assert recs[0]["status"] == "todo"
    assert recs[0]["assignees"] == ["u-2", "u-3"]
    assert recs[0]["due_date"] == "2026-04-30"
    assert isinstance(recs[0]["recurrence"], dict)
    assert recs[0]["recurrence"]["rrule"] == "FREQ=DAILY"


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
        async def list_by_space(self, space_id):
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
        async def list(self, *, space_id):
            return [page]

    recs = await PagesExporter(_Repo()).list_records("sp-1")
    assert recs[0]["id"] == "pg-1"


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
        async def list(self, *, space_id):
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
        async def list_events_in_range(self, space_id, *, start, end):
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
        async def list_albums(self, space_id, *, limit=30, before=None):
            return [album]

        async def list_items(self, album_id, *, limit=50, before=None):
            return [item]

    recs = await GalleryExporter(_Repo()).list_records("sp-1")
    assert len(recs) == 2
    assert recs[0]["kind"] == "album"
    assert recs[1]["kind"] == "item"


async def test_polls_exporter_walks_posts_with_polls():
    from socialhome.federation.sync.space.exporters import PollsExporter

    post = SimpleNamespace(id="p-1")

    class _Posts:
        async def list_for_sync(self, space_id, *, limit):
            return [post]

    class _Polls:
        async def get_meta(self, post_id):
            return {"question": "Pizza?"}

        async def list_options_with_counts(self, post_id):
            return [{"id": "opt-a", "text": "Yes", "count": 3}]

    recs = await PollsExporter(_Polls(), _Posts()).list_records("sp-1")
    assert recs[0]["post_id"] == "p-1"
    assert recs[0]["meta"]["question"] == "Pizza?"
    assert recs[0]["options"][0]["id"] == "opt-a"


async def test_polls_exporter_skips_posts_without_polls():
    from socialhome.federation.sync.space.exporters import PollsExporter

    post = SimpleNamespace(id="p-2")

    class _Posts:
        async def list_for_sync(self, space_id, *, limit):
            return [post]

    class _Polls:
        async def get_meta(self, post_id):
            return None

        async def list_options_with_counts(self, post_id):
            return []

    recs = await PollsExporter(_Polls(), _Posts()).list_records("sp-1")
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
        async def list_for_space(self, space_id):
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
        async def list_in_space(self, space_id, *, limit=2000):
            return [listing]

    recs = await BazaarExporter(_Repo()).list_records("sp-1")
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
        async def list_for_sync(self, space_id, limit=1000):
            return [post]

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

    recs = await SchedulesExporter(_PollRepo(), _PostRepo()).list_records("sp-1")
    assert len(recs) == 1
    r = recs[0]
    assert r["post_id"] == "p-sched"
    assert r["title"] == "Picnic?"
    assert len(r["slots"]) == 2
    assert r["slots"][0]["id"] == "s1"
    assert r["slots"][1]["start_time"] is None

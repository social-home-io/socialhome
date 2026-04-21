"""Unit tests for each resource exporter.

These exercise the record-serialisation paths — covering enum → str,
datetime → ISO, frozenset → list, nested dataclass → dict conversions
so the receiver has JSON-serialisable input.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace


from social_home.domain.calendar import CalendarEvent
from social_home.domain.page import Page
from social_home.domain.post import Comment, CommentType, Post, PostType
from social_home.domain.space import SpaceMember
from social_home.domain.sticky import Sticky
from social_home.domain.task import RecurrenceRule, Task, TaskStatus


class _FakeSpacePostRepo:
    def __init__(self, posts, comments_by_post=None):
        self._posts = posts
        self._comments_by_post = comments_by_post or {}

    async def list_feed(self, space_id, *, before=None, limit=20):
        return self._posts

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
    from social_home.federation.sync.space.exporters import MembersExporter

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
    from social_home.federation.sync.space.exporters import BansExporter

    ex = BansExporter(
        _FakeSpaceRepo(
            bans=[
                {"user_id": "u-x", "banned_by": "admin", "reason": "spam"},
            ]
        )
    )
    assert (await ex.list_records("sp-1"))[0]["user_id"] == "u-x"


async def test_posts_exporter_serialises_enums_and_datetimes():
    from social_home.federation.sync.space.exporters import PostsExporter

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


async def test_comments_exporter_walks_posts():
    from social_home.federation.sync.space.exporters import CommentsExporter

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
    from social_home.federation.sync.space.exporters import TasksExporter

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


async def test_tasks_archived_filters_to_done():
    from social_home.federation.sync.space.exporters import TasksArchivedExporter

    now = datetime.now(timezone.utc)
    active = Task(
        id="t-1",
        list_id="l",
        title="X",
        status=TaskStatus.TODO,
        position=0,
        created_by="u-1",
        created_at=now,
        updated_at=now,
    )
    done = Task(
        id="t-2",
        list_id="l",
        title="Y",
        status=TaskStatus.DONE,
        position=1,
        created_by="u-1",
        created_at=now,
        updated_at=now,
    )

    class _Repo:
        async def list_by_space(self, space_id):
            return [active, done]

    recs = await TasksArchivedExporter(_Repo()).list_records("sp-1")
    assert len(recs) == 1
    assert recs[0]["id"] == "t-2"


async def test_pages_exporter():
    from social_home.federation.sync.space.exporters import PagesExporter

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
    from social_home.federation.sync.space.exporters import StickiesExporter

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
    from social_home.federation.sync.space.exporters import CalendarExporter

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
    from social_home.domain.gallery import GalleryAlbum, GalleryItem
    from social_home.federation.sync.space.exporters import GalleryExporter

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
    from social_home.federation.sync.space.exporters import PollsExporter

    post = SimpleNamespace(id="p-1")

    class _Posts:
        async def list_feed(self, space_id, *, limit):
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
    from social_home.federation.sync.space.exporters import PollsExporter

    post = SimpleNamespace(id="p-2")

    class _Posts:
        async def list_feed(self, space_id, *, limit):
            return [post]

    class _Polls:
        async def get_meta(self, post_id):
            return None

        async def list_options_with_counts(self, post_id):
            return []

    recs = await PollsExporter(_Polls(), _Posts()).list_records("sp-1")
    assert recs == []

"""Unit tests for :class:`SpaceSyncResumeProvider` (spec §4.4 / §11452)."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.federation import FederationEventType
from socialhome.domain.gallery import GalleryAlbum, GalleryItem
from socialhome.domain.page import Page, PageTombstone
from socialhome.domain.post import Comment, CommentType, LocationData, Post, PostType
from socialhome.domain.sticky import Sticky
from socialhome.domain.task import (
    Task,
    TaskList,
    TaskListTombstone,
    TaskPriority,
    TaskStatus,
    TaskTombstone,
)
from socialhome.domain.tombstone import SpaceRowTombstone
from socialhome.federation.sync.space import file_gate as file_gate_mod
from socialhome.federation.sync.space import resume as resume_mod
from socialhome.federation.sync.space.file_gate import SpaceFileBudget
from socialhome.federation.sync.space.resume import (
    MAX_PER_RESOURCE,
    SpaceSyncResumeProvider,
    _post_to_payload,
)


@pytest.fixture(autouse=True)
def _replays_reach_back_to_the_fixtures(monkeypatch):
    """The fixtures date from April 2026: lift the 30-day ``since`` floor
    (its own test sets it back)."""
    monkeypatch.setattr(resume_mod, "RESUME_MAX_AGE_S", 100 * 365 * 86_400)


class _FakeFederation:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        #: ``send_with_mesh_fallback`` sends (the resume request).
        self.routed: list[dict] = []
        self.route_ok = True
        #: Advertised proto_version per peer (default: current).
        self.versions: dict[str, int] = {}

    async def peer_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 48) >= min_version

    async def space_member_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 48) >= min_version

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
                "space_id": space_id,
            }
        )

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.routed.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
                "space_id": space_id,
            }
        )
        return SimpleNamespace(ok=self.route_ok, error=None if self.route_ok else "x")


class _FakeSpaceRepo:
    def __init__(self, members: list[str]) -> None:
        self._members = members

    async def list_member_instances(self, space_id: str) -> list[str]:
        return list(self._members)

    async def get(self, space_id: str):
        # Hosted elsewhere: this provider is a member household.
        return SimpleNamespace(id=space_id, owner_instance_id="the-host")


class _FakePostRepo:
    """Posts + comments live on the same repo to mirror prod."""

    def __init__(
        self,
        posts: list[Post] | None = None,
        comments: list[tuple[str, Comment]] | None = None,
    ) -> None:
        self._posts = posts or []
        self._comments = comments or []
        self.last_since: str | None = None
        self.last_limit: int | None = None

    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[Post]:
        self.last_since = since
        self.last_limit = limit
        return [p for p in self._posts if p.created_at.isoformat() > since][:limit]

    async def list_comments_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[tuple[str, Comment]]:
        return [
            (post_id, c)
            for post_id, c in self._comments
            if c.created_at.isoformat() > since
        ][:limit]


class _FakeListSinceRepo:
    """Generic stub for repos whose since-method is just ``list_since``."""

    def __init__(
        self,
        rows: list,
        *,
        method: str = "list_since",
        lists: list | None = None,
        tombstones: list | None = None,
        task_tombstones: list | None = None,
        page_tombstones: list | None = None,
    ) -> None:
        self._rows = rows
        self._method = method
        self._lists = lists or []
        self._tombstones = tombstones or []
        self._task_tombstones = task_tombstones or []
        self._page_tombstones = page_tombstones or []

    def __getattr__(self, name):  # type: ignore[no-redef]
        if name == "list_list_tombstones":

            async def _tombstones(space_id, *, since=None, limit=500):
                return self._tombstones[:limit]

            return _tombstones
        if name == "list_task_tombstones":

            async def _task_tombstones(space_id, *, since=None, limit=500):
                return self._task_tombstones[:limit]

            return _task_tombstones
        if name == "list_page_tombstones":

            async def _page_tombstones(space_id, *, since=None, limit=500):
                return self._page_tombstones[:limit]

            return _page_tombstones
        if name == "list_lists_since":

            async def _lists(space_id, since, *, limit=500):
                return self._lists[:limit]

            return _lists
        if name == "get_draft_base":

            async def _base(page_id, *, space_id):
                return getattr(self, "bases", {}).get(page_id)

            return _base
        if name == "list_conflict_sides":

            async def _sides(page_id, *, space_id):
                return []

            return _sides
        if name in ("list_since", "list_events_since", "list_items_since"):

            async def _impl(space_id, since, *, limit=500):
                return [r for r in self._rows if _ts_attr(r) > since][:limit]

            return _impl
        raise AttributeError(name)


class _FakeGalleryRepo(_FakeListSinceRepo):
    """``list_items_since`` plus ``get_album`` for the album-before-item replay."""

    def __init__(
        self,
        rows: list,
        albums: dict[str, GalleryAlbum],
        tombstones: list[SpaceRowTombstone] | None = None,
    ) -> None:
        super().__init__(rows)
        self._albums = albums
        self._tombstones = tombstones or []

    async def list_album_tombstones_since(self, space_id, since):
        cutoff = datetime.fromisoformat(since)
        return [
            t for t in self._tombstones if datetime.fromisoformat(t.deleted_at) > cutoff
        ]

    async def get_album(self, album_id: str) -> GalleryAlbum | None:
        return self._albums.get(album_id)

    async def list_albums(self, space_id, *, limit=30, before=None):
        return [a for a in self._albums.values() if a.space_id == space_id][:limit]


def _ts_attr(row) -> str:
    """Best-effort timestamp accessor for the fake list_since stub."""
    for attr in ("updated_at", "created_at", "start"):
        v = getattr(row, attr, None)
        if v is None:
            continue
        return v.isoformat() if isinstance(v, datetime) else str(v)
    return ""


def _post(i: int, at: datetime) -> Post:
    return Post(
        id=f"p-{i}",
        author="alice",
        type=PostType.TEXT,
        created_at=at,
        content=f"hello {i}",
    )


def _comment(i: int, post_id: str, at: datetime) -> Comment:
    return Comment(
        id=f"c-{i}",
        post_id=post_id,
        author="alice",
        type=CommentType.TEXT,
        created_at=at,
        content=f"reply {i}",
    )


def _task(i: int, at: datetime) -> Task:
    return Task(
        id=f"t-{i}",
        list_id="list-1",
        title=f"task {i}",
        status=TaskStatus.TODO,
        position=i,
        created_by="alice",
        created_at=at,
        updated_at=at,
    )


def _page(i: int, iso: str) -> Page:
    return Page(
        id=f"pg-{i}",
        title=f"page {i}",
        content="...",
        created_by="alice",
        created_at=iso,
        updated_at=iso,
        space_id="sp-1",
    )


def _sticky(i: int, iso: str) -> Sticky:
    return Sticky(
        id=f"st-{i}",
        author="alice",
        content=f"note {i}",
        color="yellow",
        position_x=0.0,
        position_y=0.0,
        created_at=iso,
        updated_at=iso,
        space_id="sp-1",
    )


def _cal_event(i: int, at: datetime) -> CalendarEvent:
    return CalendarEvent(
        id=f"ev-{i}",
        calendar_id="cal-1",
        summary=f"event {i}",
        start=at,
        end=at + timedelta(hours=1),
        created_by="alice",
    )


def _gallery_item(i: int, iso: str) -> GalleryItem:
    return GalleryItem(
        id=f"gi-{i}",
        album_id="alb-1",
        uploaded_by="alice",
        item_type="photo",
        url=f"/api/media/orig-{i}.jpg",
        thumbnail_url=f"/api/media/thumb-{i}.jpg",
        width=800,
        height=600,
        created_at=iso,
    )


def _event(from_instance: str, payload: dict, *, space_id: str = "sp-1"):
    return SimpleNamespace(
        event_type=FederationEventType.SPACE_SYNC_RESUME,
        from_instance=from_instance,
        space_id=space_id,
        payload=payload,
    )


@pytest.fixture
def provider_factory():
    """Build a ``SpaceSyncResumeProvider`` with the given content + members."""

    def _factory(
        *,
        posts=None,
        comments=None,
        tasks=None,
        pages=None,
        stickies=None,
        cal_events=None,
        gallery_items=None,
        gallery_albums=None,
        gallery_tombstones=None,
        members,
    ):
        fed = _FakeFederation()
        post_repo = _FakePostRepo(posts=posts, comments=comments)
        provider = SpaceSyncResumeProvider(
            federation_service=fed,
            space_repo=_FakeSpaceRepo(members),
            space_post_repo=post_repo,
            space_task_repo=(_FakeListSinceRepo(tasks) if tasks is not None else None),
            page_repo=(_FakeListSinceRepo(pages) if pages is not None else None),
            sticky_repo=(
                _FakeListSinceRepo(stickies) if stickies is not None else None
            ),
            space_calendar_repo=(
                _FakeListSinceRepo(cal_events) if cal_events is not None else None
            ),
            gallery_repo=(
                _FakeGalleryRepo(
                    gallery_items, gallery_albums or {}, gallery_tombstones
                )
                if gallery_items is not None
                else None
            ),
        )
        return provider, fed, post_repo

    return _factory


# ── Inbound (provider replays missed events) ────────────────────────


async def test_handle_request_replays_posts_since(provider_factory):
    """Each post newer than ``since`` is re-emitted as SPACE_POST_CREATED.

    ``since`` is exclusive — an equal timestamp is filtered out, matching
    the SQL ``created_at > ?`` clause in ``list_since``.
    """
    base = datetime(2026, 4, 1, tzinfo=UTC)
    # Three posts strictly after ``base``.
    posts = [_post(i, base + timedelta(minutes=i + 1)) for i in range(3)]
    provider, fed, _ = provider_factory(posts=posts, members=["peer-a"])

    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 3
    types = {s["type"] for s in fed.sent}
    assert types == {FederationEventType.SPACE_POST_CREATED}
    assert [s["payload"]["id"] for s in fed.sent] == ["p-0", "p-1", "p-2"]
    assert all(s["to"] == "peer-a" for s in fed.sent)
    assert all(s["space_id"] == "sp-1" for s in fed.sent)


async def test_handle_request_drops_non_member(provider_factory):
    """Spec §S-1 — peer that isn't a space member is silently dropped."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    posts = [_post(0, base + timedelta(minutes=1))]
    provider, fed, _ = provider_factory(
        posts=posts,
        members=["someone-else"],
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 0
    assert fed.sent == []


async def test_handle_request_rejects_malformed_since(provider_factory):
    """A non-ISO-8601 ``since`` is dropped before the SQL query runs."""
    provider, fed, post_repo = provider_factory(
        posts=[],
        members=["peer-a"],
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": "yesterday"}),
    )
    assert sent == 0
    assert fed.sent == []
    # The repo was never queried.
    assert post_repo.last_since is None


async def test_handle_request_missing_fields(provider_factory):
    """Empty space_id or since → no-op, no replay."""
    provider, fed, _ = provider_factory(
        posts=[_post(0, datetime(2026, 4, 1, tzinfo=UTC))],
        members=["peer-a"],
    )
    # Both event.space_id and payload['space_id'] absent → drop.
    assert (
        await provider.handle_request(
            _event("peer-a", {"since": "2026-04-01T00:00:00+00:00"}, space_id=""),
        )
        == 0
    )
    # Missing 'since' → drop.
    assert await provider.handle_request(_event("peer-a", {"space_id": "sp-1"})) == 0
    assert fed.sent == []


async def test_handle_request_uses_max_posts_cap(provider_factory):
    """Repo query is bounded by MAX_PER_RESOURCE."""
    provider, _, post_repo = provider_factory(
        posts=[],
        members=["peer-a"],
    )
    await provider.handle_request(
        _event(
            "peer-a",
            {"space_id": "sp-1", "since": "2026-04-01T00:00:00+00:00"},
        ),
    )
    assert post_repo.last_limit == MAX_PER_RESOURCE


async def test_handle_request_payload_matches_inbound_shape(provider_factory):
    """Replayed payload keys match what _post_from_payload expects."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    posts = [_post(0, base + timedelta(seconds=1))]
    provider, fed, _ = provider_factory(posts=posts, members=["peer-a"])
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    payload = fed.sent[0]["payload"]
    # Same keys the SPACE_POST_CREATED inbound handler reads.
    assert {"id", "author", "type", "content", "occurred_at"} <= set(payload)
    assert payload["type"] == "text"


# ── Outbound (requester sends RESUME) ───────────────────────────────


async def test_send_request_validates_inputs(provider_factory):
    """Empty fields produce a no-op, not an error."""
    provider, fed, _ = provider_factory(posts=[], members=[])
    assert not await provider.send_request(space_id="", instance_id="peer-b", since="x")
    assert not await provider.send_request(space_id="sp-1", instance_id="", since="x")
    assert not await provider.send_request(
        space_id="sp-1", instance_id="peer-b", since=""
    )
    assert fed.sent == []
    assert fed.routed == []


# ── Multi-resource replays ──────────────────────────────────────────


async def test_handle_request_replays_comments(provider_factory):
    """Comments newer than ``since`` go out as SPACE_COMMENT_CREATED."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    comments = [
        ("p-1", _comment(0, "p-1", base + timedelta(minutes=1))),
        ("p-2", _comment(1, "p-2", base + timedelta(minutes=2))),
    ]
    provider, fed, _ = provider_factory(comments=comments, members=["peer-a"])
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    types = {s["type"] for s in fed.sent}
    assert types == {FederationEventType.SPACE_COMMENT_CREATED}
    payload0 = fed.sent[0]["payload"]
    assert payload0["post_id"] == "p-1"
    assert payload0["comment_id"] == "c-0"


async def test_handle_request_replays_tasks(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=UTC)
    tasks = [_task(i, base + timedelta(minutes=i + 1)) for i in range(2)]
    provider, fed, _ = provider_factory(tasks=tasks, members=["peer-a"])
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert all(s["type"] == FederationEventType.SPACE_TASK_CREATED for s in fed.sent)
    payload = fed.sent[0]["payload"]
    # Match the keys SPACE_TASK_* inbound reads.
    assert {"id", "list_id", "title", "status", "created_by"} <= set(payload)
    # The shared wire codec — the replay used to drop the due date.
    assert {"due_date", "archived_at", "priority", "labels"} <= set(payload)
    assert payload["space_id"] == "sp-1"


async def test_handle_request_replays_task_lists_before_tasks():
    """v_40: the lists a peer missed go out (as SPACE_TASK_LIST_CREATED)
    before the tasks filed under them."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    fed = _FakeFederation()
    provider = SpaceSyncResumeProvider(
        federation_service=fed,
        space_repo=_FakeSpaceRepo(["peer-a"]),
        space_post_repo=_FakePostRepo(),
        space_task_repo=_FakeListSinceRepo(
            [_task(0, base + timedelta(minutes=1))],
            lists=[TaskList(id="l-1", name="Chores", created_by="u-1")],
        ),
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert [s["type"] for s in fed.sent] == [
        FederationEventType.SPACE_TASK_LIST_CREATED,
        FederationEventType.SPACE_TASK_CREATED,
    ]
    assert fed.sent[0]["payload"] == {
        "id": "l-1",
        "space_id": "sp-1",
        "name": "Chores",
        "created_by": "u-1",
    }


async def test_handle_request_replays_list_deletes_before_lists_and_tasks():
    """Migration 0069: a list deleted since ``since`` goes out as
    SPACE_TASK_LIST_DELETED ``{id, space_id}`` ahead of the live lists."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    fed = _FakeFederation()
    provider = SpaceSyncResumeProvider(
        federation_service=fed,
        space_repo=_FakeSpaceRepo(["peer-a"]),
        space_post_repo=_FakePostRepo(),
        space_task_repo=_FakeListSinceRepo(
            [],
            lists=[TaskList(id="l-1", name="Chores", created_by="u-1")],
            tombstones=[
                TaskListTombstone(
                    id="l-gone",
                    deleted_at="2026-04-02",
                    created_by="u-1",
                    deleted_by="u-adm",
                )
            ],
        ),
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert [s["type"] for s in fed.sent] == [
        FederationEventType.SPACE_TASK_LIST_DELETED,
        FederationEventType.SPACE_TASK_LIST_CREATED,
    ]
    # The deleter rides as ``actor_user_id`` so a restricted level can judge
    # the replayed delete; ``created_by`` binds the id to its space.
    assert fed.sent[0]["payload"] == {
        "id": "l-gone",
        "space_id": "sp-1",
        "created_by": "u-1",
        "actor_user_id": "u-adm",
    }


async def test_handle_request_replays_task_deletes_after_lists_before_tasks():
    """Migration 0071: a task deleted since ``since`` goes out as
    SPACE_TASK_DELETED after the lists (its list is held by then) and
    before the live tasks, naming the deleter as ``actor_user_id``."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    fed = _FakeFederation()
    provider = SpaceSyncResumeProvider(
        federation_service=fed,
        space_repo=_FakeSpaceRepo(["peer-a"]),
        space_post_repo=_FakePostRepo(),
        space_task_repo=_FakeListSinceRepo(
            [
                Task(
                    id="t-live",
                    list_id="l-1",
                    title="Live",
                    status=TaskStatus.TODO,
                    position=0,
                    created_by="u-1",
                    created_at=base + timedelta(hours=1),
                    updated_at=base + timedelta(hours=1),
                )
            ],
            lists=[TaskList(id="l-1", name="Chores", created_by="u-1")],
            task_tombstones=[
                TaskTombstone(
                    id="t-gone",
                    list_id="l-1",
                    deleted_at="2026-04-02",
                    created_by="u-1",
                    deleted_by="u-adm",
                )
            ],
        ),
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 3
    assert [s["type"] for s in fed.sent] == [
        FederationEventType.SPACE_TASK_LIST_CREATED,
        FederationEventType.SPACE_TASK_DELETED,
        FederationEventType.SPACE_TASK_CREATED,
    ]
    assert fed.sent[1]["payload"] == {
        "id": "t-gone",
        "space_id": "sp-1",
        "list_id": "l-1",
        "created_by": "u-1",
        "actor_user_id": "u-adm",
    }


async def test_handle_request_replays_page_deletes_before_the_pages():
    """Migration 0073: a page deleted since ``since`` goes out as
    SPACE_PAGE_DELETED before the live pages, naming the deleter as
    ``actor_user_id`` and binding the id with ``created_by``."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    fed = _FakeFederation()
    provider = SpaceSyncResumeProvider(
        federation_service=fed,
        space_repo=_FakeSpaceRepo(["peer-a"]),
        space_post_repo=_FakePostRepo(),
        page_repo=_FakeListSinceRepo(
            [_page(0, (base + timedelta(minutes=1)).isoformat())],
            page_tombstones=[
                PageTombstone(
                    id="pg-gone",
                    deleted_at="2026-04-02 00:00:00",
                    created_by="u-1",
                    deleted_by="u-adm",
                )
            ],
        ),
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert [s["type"] for s in fed.sent] == [
        FederationEventType.SPACE_PAGE_DELETED,
        FederationEventType.SPACE_PAGE_CREATED,
    ]
    assert fed.sent[0]["payload"] == {
        "id": "pg-gone",
        "page_id": "pg-gone",
        "space_id": "sp-1",
        "created_by": "u-1",
        "actor_user_id": "u-adm",
    }


async def test_handle_request_replays_task_due_date_priority_labels(
    provider_factory,
):
    base = datetime(2026, 4, 1, tzinfo=UTC)
    task = copy.replace(
        _task(0, base + timedelta(minutes=1)),
        due_date=date(2026, 5, 2),
        priority=TaskPriority.HIGH,
        labels=("Trip",),
    )
    provider, fed, _ = provider_factory(tasks=[task], members=["peer-a"])
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    payload = fed.sent[0]["payload"]
    assert payload["due_date"] == "2026-05-02"
    assert payload["priority"] == "high"
    assert payload["labels"] == ["Trip"]


async def test_handle_request_replays_pages(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    pages = [_page(0, iso), _page(1, iso)]
    provider, fed, _ = provider_factory(pages=pages, members=["peer-a"])
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert all(s["type"] == FederationEventType.SPACE_PAGE_CREATED for s in fed.sent)


async def test_replayed_pages_carry_seq_for_a_v48_peer_only(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    page = replace(_page(0, iso), seq=6)
    provider, fed, _ = provider_factory(pages=[page], members=["peer-a", "peer-b"])
    fed.versions["peer-b"] = 47
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    await provider.handle_request(
        _event("peer-b", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    by_peer = {s["to"]: s["payload"] for s in fed.sent}
    assert by_peer["peer-a"]["seq"] == 6
    assert by_peer["peer-a"]["conflict"] == []
    assert "seq" not in by_peer["peer-b"]


async def test_no_page_is_replayed_to_the_v48_host(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    provider, fed, _ = provider_factory(pages=[_page(0, iso)], members=["the-host"])
    await provider.handle_request(
        _event("the-host", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert not [
        s for s in fed.sent if s["type"] is FederationEventType.SPACE_PAGE_CREATED
    ]


async def test_a_draft_is_replayed_as_its_canonical_base(provider_factory):
    from socialhome.domain.page_version import DraftBase

    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    draft = replace(_page(0, iso), content="my draft", seq=4, pending_base_seq=3)
    create = replace(_page(1, iso), seq=0, pending_base_seq=0)
    provider, fed, _ = provider_factory(pages=[draft, create], members=["peer-a"])
    provider._page_repo.bases = {
        draft.id: DraftBase(title="T", content="canonical", seq=3, by="u"),
        create.id: DraftBase(title="", content="", seq=0, by="u"),
    }
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    pages = [
        s["payload"]
        for s in fed.sent
        if s["type"] is FederationEventType.SPACE_PAGE_CREATED
    ]
    assert [(p["content"], p["seq"]) for p in pages] == [("canonical", 3)]


async def test_handle_request_replays_stickies(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    stickies = [_sticky(0, iso), _sticky(1, iso)]
    provider, fed, _ = provider_factory(stickies=stickies, members=["peer-a"])
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert all(s["type"] == FederationEventType.SPACE_STICKY_CREATED for s in fed.sent)


async def test_handle_request_replays_calendar_events(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=UTC)
    events = [
        _cal_event(0, base + timedelta(minutes=1)),
        _cal_event(1, base + timedelta(minutes=2)),
    ]
    provider, fed, _ = provider_factory(cal_events=events, members=["peer-a"])
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert all(
        s["type"] == FederationEventType.SPACE_CALENDAR_EVENT_CREATED for s in fed.sent
    )
    payload = fed.sent[0]["payload"]
    assert {"id", "calendar_id", "summary", "start", "end"} <= set(payload)


async def test_handle_request_replays_gallery_items(provider_factory):
    """Gallery items newer than ``since`` go out as SPACE_GALLERY_ITEM_CREATED."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    items = [_gallery_item(0, iso), _gallery_item(1, iso)]
    provider, fed, _ = provider_factory(
        gallery_items=items,
        members=["peer-a"],
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert all(
        s["type"] == FederationEventType.SPACE_GALLERY_ITEM_CREATED for s in fed.sent
    )
    payload = fed.sent[0]["payload"]
    # Same shape as the live push — the full ``url`` included.
    assert payload["url"] == "/api/media/orig-0.jpg"
    assert {"id", "album_id", "uploaded_by", "thumbnail_url"} <= set(payload)


async def test_replayed_items_are_preceded_by_their_album(provider_factory):
    """An item lands only in an album the receiver already holds, and an
    album created after it went offline is exactly what it lacks — so each
    replayed item's album goes out first, once. The system album never
    does: every household rebuilds its own from the posts."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    items = [
        _gallery_item(0, iso),
        _gallery_item(1, iso),
        GalleryItem(
            id="gi-sys",
            album_id="alb-sys",
            uploaded_by="alice",
            item_type="photo",
            url="/api/media/s.jpg",
            thumbnail_url="/api/media/s-t.jpg",
            width=1,
            height=1,
            created_at=iso,
        ),
        GalleryItem(
            id="gi-gone",
            album_id="alb-gone",
            uploaded_by="alice",
            item_type="photo",
            url="/api/media/g.jpg",
            thumbnail_url="/api/media/g-t.jpg",
            width=1,
            height=1,
            created_at=iso,
        ),
    ]
    albums = {
        "alb-1": GalleryAlbum(
            id="alb-1", space_id="sp-1", owner_user_id="u-remote", name="Trip"
        ),
        "alb-sys": GalleryAlbum(
            id="alb-sys",
            space_id="sp-1",
            owner_user_id=None,
            name="Posts",
            is_system=True,
        ),
    }
    provider, fed, _ = provider_factory(
        gallery_items=items, gallery_albums=albums, members=["peer-a"]
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    types = [s["type"] for s in fed.sent]
    assert types[0] == FederationEventType.SPACE_GALLERY_ALBUM_CREATED
    assert types.count(FederationEventType.SPACE_GALLERY_ALBUM_CREATED) == 1
    assert types[1:] == [FederationEventType.SPACE_GALLERY_ITEM_CREATED] * 4
    album_payload = fed.sent[0]["payload"]
    assert (album_payload["id"], album_payload["owner_user_id"]) == (
        "alb-1",
        "u-remote",
    )
    assert sent == 5


async def test_handle_request_aggregates_across_resources(provider_factory):
    """The total return value sums every resource type."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    cutoff = base.isoformat()
    iso = (base + timedelta(minutes=1)).isoformat()
    provider, fed, _ = provider_factory(
        posts=[_post(0, base + timedelta(minutes=1))],
        comments=[("p-X", _comment(0, "p-X", base + timedelta(minutes=1)))],
        tasks=[_task(0, base + timedelta(minutes=1))],
        pages=[_page(0, iso)],
        stickies=[_sticky(0, iso)],
        cal_events=[_cal_event(0, base + timedelta(minutes=1))],
        gallery_items=[_gallery_item(0, iso)],
        members=["peer-a"],
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": cutoff}),
    )
    assert sent == 7
    assert {s["type"] for s in fed.sent} == {
        FederationEventType.SPACE_POST_CREATED,
        FederationEventType.SPACE_COMMENT_CREATED,
        FederationEventType.SPACE_TASK_CREATED,
        FederationEventType.SPACE_PAGE_CREATED,
        FederationEventType.SPACE_STICKY_CREATED,
        FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
        FederationEventType.SPACE_GALLERY_ITEM_CREATED,
    }


async def test_handle_request_skips_resources_when_repo_missing(provider_factory):
    """Optional repos default to ``None`` — provider just skips them."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    # Only the post repo is provided; the others stay None on the provider.
    provider, fed, _ = provider_factory(
        posts=[_post(0, base + timedelta(minutes=1))],
        members=["peer-a"],
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 1
    assert fed.sent[0]["type"] == FederationEventType.SPACE_POST_CREATED


# ── replay_space_to / replay_calendar_to (resync entry points) ──────


async def test_replay_space_to_replays_all_resources(provider_factory):
    """``replay_space_to`` mirrors a full ``handle_request`` for a member."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    provider, fed, _ = provider_factory(
        posts=[_post(0, base + timedelta(minutes=1))],
        comments=[("p-X", _comment(0, "p-X", base + timedelta(minutes=1)))],
        tasks=[_task(0, base + timedelta(minutes=1))],
        pages=[_page(0, iso)],
        stickies=[_sticky(0, iso)],
        cal_events=[_cal_event(0, base + timedelta(minutes=1))],
        gallery_items=[_gallery_item(0, iso)],
        members=["peer-a"],
    )
    sent = await provider.replay_space_to(
        space_id="sp-1",
        instance_id="peer-a",
        since=base.isoformat(),
    )
    assert sent == 7
    assert all(s["to"] == "peer-a" for s in fed.sent)


async def test_replay_space_to_drops_non_member(provider_factory):
    """Security boundary — a non-member never receives space content."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    provider, fed, _ = provider_factory(
        posts=[_post(0, base + timedelta(minutes=1))],
        members=["someone-else"],
    )
    sent = await provider.replay_space_to(
        space_id="sp-1",
        instance_id="peer-a",
        since=base.isoformat(),
    )
    assert sent == 0
    assert fed.sent == []


async def test_replay_calendar_to_replays_only_calendar(provider_factory):
    """``replay_calendar_to`` re-emits calendar events only."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    iso = (base + timedelta(minutes=1)).isoformat()
    provider, fed, _ = provider_factory(
        posts=[_post(0, base + timedelta(minutes=1))],
        cal_events=[_cal_event(0, base + timedelta(minutes=1))],
        gallery_items=[_gallery_item(0, iso)],
        members=["peer-a"],
    )
    sent = await provider.replay_calendar_to(
        space_id="sp-1",
        instance_id="peer-a",
        since=base.isoformat(),
    )
    assert sent == 1
    assert [s["type"] for s in fed.sent] == [
        FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
    ]
    assert fed.sent[0]["to"] == "peer-a"


async def test_replay_calendar_to_drops_non_member(provider_factory):
    """Security boundary — a non-member never receives the calendar."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    provider, fed, _ = provider_factory(
        cal_events=[_cal_event(0, base + timedelta(minutes=1))],
        members=["someone-else"],
    )
    sent = await provider.replay_calendar_to(
        space_id="sp-1",
        instance_id="peer-a",
        since=base.isoformat(),
    )
    assert sent == 0
    assert fed.sent == []


# ── _post_to_payload location handling ─────────────────────────────────


def test_post_to_payload_omits_location_when_unset():
    """A normal post (no location) doesn't pollute the wire payload."""
    post = Post(
        id="p-1",
        author="alice",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        content="hi",
    )
    payload = _post_to_payload(post)
    assert "location" not in payload


def test_post_to_payload_includes_location_with_label():
    """LOCATION posts ride on SPACE_POST_CREATED with a `location`
    sub-block that includes lat/lon/label."""
    post = Post(
        id="p-loc",
        author="alice",
        type=PostType.LOCATION,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        content="Sunset",
        location=LocationData(lat=52.52, lon=4.06, label="Marina"),
    )
    payload = _post_to_payload(post)
    assert payload["type"] == "location"
    assert payload["location"] == {"lat": 52.52, "lon": 4.06, "label": "Marina"}


def test_post_to_payload_omits_label_when_none():
    """A LocationData with label=None ⇒ no `label` key on the wire."""
    post = Post(
        id="p-loc-2",
        author="alice",
        type=PostType.LOCATION,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        location=LocationData(lat=10.0, lon=20.0),
    )
    payload = _post_to_payload(post)
    assert payload["location"] == {"lat": 10.0, "lon": 20.0}


async def test_resume_replays_every_album_state_the_peer_may_have_missed(
    provider_factory,
):
    """Albums are replayed whether or not anything was uploaded to them since
    — an empty album made while the peer was away is just as missing. One
    edited since ``since`` also goes out as an update (a create for an
    album the peer holds is a no-op), and a delete recorded since then is
    replayed first."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    since = base.isoformat()
    later = (base + timedelta(minutes=5)).isoformat()
    earlier = (base - timedelta(days=1)).isoformat()
    albums = {
        "alb-empty": GalleryAlbum(
            id="alb-empty",
            space_id="sp-1",
            owner_user_id="u-a",
            name="Empty",
            created_at=later,
            updated_at=later,
        ),
        "alb-old": GalleryAlbum(
            id="alb-old",
            space_id="sp-1",
            owner_user_id="u-a",
            name="Old",
            created_at=earlier,
            updated_at=earlier,
        ),
        "alb-renamed": GalleryAlbum(
            id="alb-renamed",
            space_id="sp-1",
            owner_user_id="u-a",
            name="Renamed",
            created_at=earlier,
            updated_at=later,
        ),
        "alb-elsewhere": GalleryAlbum(
            id="alb-elsewhere", space_id="sp-2", owner_user_id="u-a", name="X"
        ),
    }
    tombstones = [
        SpaceRowTombstone(
            id=album_id,
            owner="u-a",
            created_at=earlier,
            deleted_at=at.isoformat(),
        )
        for album_id, at in (
            ("alb-gone", base + timedelta(minutes=1)),
            ("alb-gone-long-ago", base - timedelta(days=2)),
        )
    ]
    provider, fed, _ = provider_factory(
        gallery_items=[],
        gallery_albums=albums,
        gallery_tombstones=tombstones,
        members=["peer-a"],
    )
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": since}),
    )
    got = [(s["type"], s["payload"]["id"]) for s in fed.sent]
    # The replayed delete names the owner, so a receiver that never held the
    # album can bind its owner-bound id before remembering the delete.
    assert fed.sent[0]["payload"] == {"id": "alb-gone", "owner_user_id": "u-a"}
    assert got == [
        (FederationEventType.SPACE_GALLERY_ALBUM_DELETED, "alb-gone"),
        (FederationEventType.SPACE_GALLERY_ALBUM_CREATED, "alb-empty"),
        (FederationEventType.SPACE_GALLERY_ALBUM_CREATED, "alb-old"),
        (FederationEventType.SPACE_GALLERY_ALBUM_CREATED, "alb-renamed"),
        (FederationEventType.SPACE_GALLERY_ALBUM_UPDATED, "alb-empty"),
        (FederationEventType.SPACE_GALLERY_ALBUM_UPDATED, "alb-renamed"),
    ]
    assert sent == 6


def test_post_to_payload_carries_link_preview_only_when_set():
    """A resumed SPACE_POST_CREATED replays the author-built card so a
    catching-up member still never fetches the URL."""
    from socialhome.domain.link_preview import LinkPreview

    base = {
        "id": "p-lp",
        "author": "alice",
        "type": PostType.TEXT,
        "created_at": datetime(2026, 4, 1, tzinfo=UTC),
        "content": "https://example.com/",
    }
    assert "link_preview" not in _post_to_payload(Post(**base))
    card = LinkPreview(url="https://example.com/", title="T")
    payload = _post_to_payload(Post(**base, link_preview=card))
    assert payload["link_preview"]["title"] == "T"
    assert payload["link_preview"]["url"] == "https://example.com/"


class _FakeMediaOutbox:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []

    async def enqueue_for_blob(
        self, *, space_id, correlation_id, target_instance_ids, media_urls
    ):
        self.rows.append(
            (space_id, correlation_id, tuple(target_instance_ids), tuple(media_urls))
        )


async def test_a_replayed_rows_files_ship_from_the_replaying_household():
    """A receiver names a file by the household that delivered its row, so
    the files of every replayed post / cover / gallery item ship from us —
    to the one household that asked, under the names our rows hold."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    at = base + timedelta(minutes=1)
    post = replace(
        _post(0, at),
        type=PostType.IMAGE,
        media_url="api/media/sd-a.webp",
        image_urls=("api/media/sd-b.webp",),
    )
    event = replace(_cal_event(0, at), cover_url="api/media/cover.webp")
    outbox = _FakeMediaOutbox()
    provider = SpaceSyncResumeProvider(
        federation_service=_FakeFederation(),
        space_repo=_FakeSpaceRepo(["peer-a"]),
        space_post_repo=_FakePostRepo(posts=[post, _post(1, at)]),
        space_calendar_repo=_FakeListSinceRepo([event]),
        media_sync=outbox,
    )
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert outbox.rows == [
        ("sp-1", "p-0", ("peer-a",), ("api/media/sd-a.webp", "api/media/sd-b.webp")),
        ("sp-1", "ev-0", ("peer-a",), ("api/media/cover.webp",)),
    ]


class _AllSpaceFiles:
    async def confined_to_space(self, name, space_id):
        return name != "elsewhere.webp"


class _HostSpaceRepo(_FakeSpaceRepo):
    async def get(self, space_id: str):
        return SimpleNamespace(id=space_id, owner_instance_id="us")


@pytest.mark.parametrize(
    ("host", "version", "shipped"),
    [("us", 82, True), ("us", 80, False), ("the-host", 82, False)],
)
async def test_the_hosts_page_replay_ships_its_files_to_v81(host, version, shipped):
    """v_82: a replayed page's files (cover, body pictures) ship with it —
    only from the space's host (on a receiver only it fills the names the
    page shows), only to a household at v_82."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    at = (base + timedelta(minutes=1)).isoformat()
    page = replace(
        _page(0, at),
        content="![a](api/media/a.webp) ![e](api/media/elsewhere.webp)",
        cover_image_url="api/media/c.webp",
        seq=2,
    )
    fed = _FakeFederation()
    fed.own_instance_id = "us"
    fed.versions["peer-a"] = version
    outbox = _FakeMediaOutbox()
    spaces = (_HostSpaceRepo if host == "us" else _FakeSpaceRepo)(["peer-a"])
    provider = SpaceSyncResumeProvider(
        federation_service=fed,
        space_repo=spaces,
        space_post_repo=_FakePostRepo(),
        page_repo=_FakeListSinceRepo([page]),
        media_sync=outbox,
        media_refs=_AllSpaceFiles(),
    )
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert [s["type"] for s in fed.sent] == [FederationEventType.SPACE_PAGE_CREATED]
    expected = [
        ("sp-1", page.id, ("peer-a",), ("api/media/c.webp", "api/media/a.webp"))
    ]
    assert outbox.rows == (expected if shipped else [])


def _image_post(i: int, at: datetime, *names: str) -> Post:
    return replace(
        _post(i, at),
        type=PostType.IMAGE,
        image_urls=tuple(f"api/media/{n}" for n in names),
    )


def _bounded_provider(tmp_path, posts, outbox, clock=lambda: 0.0, budget=None):
    return SpaceSyncResumeProvider(
        federation_service=_FakeFederation(),
        space_repo=_FakeSpaceRepo(["peer-a"]),
        space_post_repo=_FakePostRepo(posts=posts),
        media_sync=outbox,
        media_dir=tmp_path,
        clock=clock,
        budget=budget,
    )


async def test_a_resumes_files_are_bounded_in_bytes_and_count(tmp_path, monkeypatch):
    """The requester picks ``since``: one request pushes at most the shared
    (requester, space) budget's files and bytes of what is here — a file
    past the budget, or not here, is skipped."""
    monkeypatch.setattr(file_gate_mod, "BUDGET_MAX_BYTES", 100)
    monkeypatch.setattr(file_gate_mod, "BUDGET_MAX_FILES", 3)
    for name, size in (("a", 40), ("big", 500), ("b", 40), ("c", 10), ("d", 1)):
        (tmp_path / f"{name}.webp").write_bytes(b"x" * size)
    base = datetime(2026, 4, 1, tzinfo=UTC)
    at = base + timedelta(minutes=1)
    posts = [
        _image_post(0, at, "a.webp", "big.webp", "gone.webp"),
        _image_post(1, at, "b.webp", "c.webp", "d.webp"),
    ]
    outbox = _FakeMediaOutbox()
    provider = _bounded_provider(tmp_path, posts, outbox)
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()})
    )
    shipped = [u for row in outbox.rows for u in row[3]]
    # 40 + 40 + 10 bytes, three files: "big" is over the bytes, "gone" is
    # missing, "d" is over the count.
    assert shipped == ["api/media/a.webp", "api/media/b.webp", "api/media/c.webp"]


async def test_a_resume_and_space_media_wanted_share_one_budget(tmp_path):
    """ONE budget per (requester, space): files the household was already
    sent on request (``SPACE_MEDIA_WANTED``) this window count against a
    resume's files too."""
    (tmp_path / "a.webp").write_bytes(b"x")
    budget = SpaceFileBudget(clock=lambda: 0.0)
    for _ in range(file_gate_mod.BUDGET_MAX_FILES):
        assert budget.take("peer-a", "sp-1", 1)
    base = datetime(2026, 4, 1, tzinfo=UTC)
    outbox = _FakeMediaOutbox()
    provider = _bounded_provider(
        tmp_path,
        [_image_post(0, base + timedelta(minutes=1), "a.webp")],
        outbox,
        budget=budget,
    )
    assert (
        await provider.handle_request(
            _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()})
        )
        == 1
    )
    assert outbox.rows == []


async def test_a_resume_is_served_once_per_household_and_space_per_cooldown(
    tmp_path,
):
    now = [1000.0]
    base = datetime(2026, 4, 1, tzinfo=UTC)
    posts = [_post(0, base + timedelta(minutes=1))]
    outbox = _FakeMediaOutbox()
    provider = _bounded_provider(tmp_path, posts, outbox, clock=lambda: now[0])
    request = _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()})
    assert await provider.handle_request(request) == 1
    now[0] += resume_mod.RESUME_COOLDOWN_S - 1
    assert await provider.handle_request(request) == 0
    now[0] += 2
    assert await provider.handle_request(request) == 1
    # Another space or household keeps its own window.
    assert (
        await provider.handle_request(
            _event(
                "peer-a",
                {"space_id": "sp-2", "since": base.isoformat()},
                space_id="sp-2",
            )
        )
        == 1
    )


async def test_a_resync_replay_shares_the_resume_cooldown(tmp_path):
    """§319.6 ``INSTANCE_RESYNC_REQUEST`` calls the replay directly with
    ``since`` = epoch: the cooldown lives in the replay itself, keyed on the
    requesting household and the space, so repeated resyncs ship the files
    once per window — whichever entry point asked."""
    now = [1000.0]
    base = datetime(2026, 4, 1, tzinfo=UTC)
    (tmp_path / "a.webp").write_bytes(b"x")
    posts = [_image_post(0, base + timedelta(minutes=1), "a.webp")]
    outbox = _FakeMediaOutbox()
    provider = _bounded_provider(tmp_path, posts, outbox, clock=lambda: now[0])
    epoch = "1970-01-01T00:00:00+00:00"
    for _ in range(3):
        await provider.replay_space_to(
            space_id="sp-1", instance_id="peer-a", since=epoch
        )
    await provider.replay_calendar_to(
        space_id="sp-1", instance_id="peer-a", since=epoch
    )
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()})
    )
    assert len(outbox.rows) == 1
    now[0] += resume_mod.RESUME_COOLDOWN_S + 1
    await provider.replay_space_to(space_id="sp-1", instance_id="peer-a", since=epoch)
    assert len(outbox.rows) == 2


async def test_concurrent_resumes_are_served_once(tmp_path):
    """The window starts before the replay awaits anything, so requests
    racing each other cannot all slip through."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    (tmp_path / "a.webp").write_bytes(b"x")
    posts = [_image_post(0, base + timedelta(minutes=1), "a.webp")]
    outbox = _FakeMediaOutbox()
    provider = _bounded_provider(tmp_path, posts, outbox)

    class _SlowFederation(_FakeFederation):
        async def send_event(self, **kw):
            await asyncio.sleep(0)  # a real send yields: requests interleave
            await super().send_event(**kw)

    provider._federation = _SlowFederation()
    request = _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()})
    sent = await asyncio.gather(*(provider.handle_request(request) for _ in range(5)))
    assert sorted(sent) == [0, 0, 0, 0, 1]
    assert len(outbox.rows) == 1


# ── v_92: the v_87 refill bridge is gone ─────────────────────────────────


async def test_a_stray_refill_field_is_an_ordinary_resume():
    """The never-released v_87 ``refill`` narrowing is gone (v_92
    ``SPACE_MEDIA_WANTED`` replaced it): the field is ignored and the request
    is the ordinary, once-per-window resume."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    at = base + timedelta(minutes=1)
    fed = _FakeFederation()
    provider = SpaceSyncResumeProvider(
        federation_service=fed,
        space_repo=_FakeSpaceRepo(["peer-a"]),
        space_post_repo=_FakePostRepo(posts=[_post(0, at)]),
    )
    stray = {"space_id": "sp-1", "since": base.isoformat(), "refill": "posts"}
    assert await provider.handle_request(_event("peer-a", stray)) == 1
    assert not await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()})
    )
    assert not hasattr(provider, "replay_refill_to")


# ── v_92: bounded on every path; a helper replays only its own people ────


async def test_a_resumes_since_is_clamped_to_30_days(monkeypatch):
    """However far back a requester asks, a replay starts at most
    :data:`RESUME_MAX_AGE_S` ago — the catch-up is bounded on every path."""
    monkeypatch.setattr(resume_mod, "RESUME_MAX_AGE_S", 30 * 86_400)
    post_repo = _FakePostRepo()
    provider = SpaceSyncResumeProvider(
        federation_service=_FakeFederation(),
        space_repo=_FakeSpaceRepo(["peer-a", "peer-b"]),
        space_post_repo=post_repo,
    )
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": "2026-01-01T00:00:00+00:00"})
    )
    floor = datetime.now(UTC) - timedelta(days=30)
    assert abs(datetime.fromisoformat(post_repo.last_since) - floor) < timedelta(
        minutes=1
    )
    # A recent ``since`` (naive form too) is kept as asked.
    recent = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    await provider.handle_request(
        _event("peer-b", {"space_id": "sp-1", "since": recent})
    )
    assert post_repo.last_since == recent


async def test_send_request_reaches_a_helper_over_the_mesh_or_its_mailbox(
    provider_factory,
):
    """A helper may be reached by mesh or a GFS mailbox seat only: the
    request goes through ``send_with_mesh_fallback`` (space-scoped)."""
    provider, fed, _ = provider_factory(posts=[], members=[])
    assert await provider.send_request(
        space_id="sp-1", instance_id="helper", since="2026-11-01T00:00:00+00:00"
    )
    fed.route_ok = False
    assert not await provider.send_request(
        space_id="sp-1", instance_id="helper", since="2026-11-01T00:00:00+00:00"
    )
    fed.routed.pop()
    assert fed.sent == []
    assert fed.routed == [
        {
            "to": "helper",
            "type": FederationEventType.SPACE_SYNC_RESUME,
            "payload": {"space_id": "sp-1", "since": "2026-11-01T00:00:00+00:00"},
            "space_id": "sp-1",
        }
    ]


def _helper_provider(*, authority: bool):
    """A moderator household's resume provider over rows of its own person
    (alice) and of another household's (bob)."""
    base = datetime(2026, 4, 1, tzinfo=UTC)
    at = base + timedelta(minutes=1)
    ours = _image_post(0, at, "sd-a.webp", "sc-a.webp")
    theirs = replace(_image_post(1, at, "sd-b.webp", "sc-b.webp"), author="bob")
    comments = [
        ("p-0", _comment(0, "p-0", at)),
        (
            "p-1",
            replace(
                _comment(1, "p-1", at),
                author="bob",
                type=CommentType.IMAGE,
                media_url="api/media/sc-c.webp",
            ),
        ),
    ]
    tasks = _FakeListSinceRepo(
        [_task(0, at), replace(_task(1, at), created_by="bob")],
        task_tombstones=[
            TaskTombstone(
                id="t-gone-ours",
                list_id="list-1",
                deleted_at="2026-04-02 00:00:00",
                created_by="bob",
                deleted_by="alice",
            ),
            TaskTombstone(
                id="t-gone-theirs",
                list_id="list-1",
                deleted_at="2026-04-02 00:00:00",
                created_by="alice",
                deleted_by="bob",
            ),
        ],
    )
    fed = _FakeFederation()
    outbox = _FakeMediaOutbox()
    provider = SpaceSyncResumeProvider(
        federation_service=fed,
        # "peer-a" holds a member seat; "follower" only follows (its
        # household is in ``space_instances`` all the same).
        space_repo=_FakeSpaceRepo(["peer-a", "follower"]),
        space_post_repo=_FakePostRepo(posts=[ours, theirs], comments=comments),
        space_task_repo=tasks,
        media_sync=outbox,
    )

    async def _authority(space_id: str) -> bool:
        return authority

    async def _own(space_id: str) -> set[str]:
        return {"alice"}

    async def _writer_seat(space_id: str, instance_id: str) -> bool:
        return instance_id == "peer-a"

    provider.attach_row_scope(_authority, own_members=_own, member_gate=_writer_seat)
    return provider, fed, outbox, base


async def test_a_non_authority_resume_sends_only_its_own_peoples_rows():
    """A helper (not the roster authority) replays only its own people's
    rows — the receiver refuses the rest from it — and ships only those
    rows' files: other people's files come on view (SPACE_MEDIA_WANTED)."""
    provider, fed, outbox, base = _helper_provider(authority=False)
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()})
    )
    sent = [
        (s["type"], s["payload"].get("id") or s["payload"].get("comment_id"))
        for s in fed.sent
    ]
    assert sent == [
        (FederationEventType.SPACE_POST_CREATED, "p-0"),
        (FederationEventType.SPACE_COMMENT_CREATED, "c-0"),
        (FederationEventType.SPACE_TASK_DELETED, "t-gone-ours"),
        (FederationEventType.SPACE_TASK_CREATED, "t-0"),
    ]
    assert outbox.rows == [
        ("sp-1", "p-0", ("peer-a",), ("api/media/sd-a.webp", "api/media/sc-a.webp")),
    ]


@pytest.mark.parametrize("authority", [False, True])
async def test_a_follower_only_household_gets_nothing_from_a_helper(authority):
    """A helper gates on a writer (member) seat — the §25.6 provider's and
    SPACE_MEDIA_WANTED's ``may_receive`` — so a household that only follows
    gets nothing from it. The roster authority keeps its own gate (a
    follower gets its subset there)."""
    provider, fed, outbox, base = _helper_provider(authority=authority)
    sent = await provider.handle_request(
        _event("follower", {"space_id": "sp-1", "since": base.isoformat()})
    )
    assert (sent > 0) is authority
    if not authority:
        assert fed.sent == [] and outbox.rows == []


async def test_the_roster_authority_resume_replays_every_row():
    provider, fed, _outbox, base = _helper_provider(authority=True)
    await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()})
    )
    ids = [s["payload"].get("id") or s["payload"].get("comment_id") for s in fed.sent]
    assert ids == [
        "p-0",
        "p-1",
        "c-0",
        "c-1",
        "t-gone-ours",
        "t-gone-theirs",
        "t-0",
        "t-1",
    ]

"""Unit tests for :class:`SpaceSyncResumeProvider` (spec §4.4 / §11452)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.federation import FederationEventType
from socialhome.domain.gallery import GalleryItem
from socialhome.domain.page import Page
from socialhome.domain.post import Comment, CommentType, LocationData, Post, PostType
from socialhome.domain.sticky import Sticky
from socialhome.domain.task import Task, TaskStatus
from socialhome.federation.sync.space.resume import (
    MAX_PER_RESOURCE,
    SpaceSyncResumeProvider,
    _post_to_payload,
)


class _FakeFederation:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
                "space_id": space_id,
            }
        )


class _FakeSpaceRepo:
    def __init__(self, members: list[str]) -> None:
        self._members = members

    async def list_member_instances(self, space_id: str) -> list[str]:
        return list(self._members)


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

    def __init__(self, rows: list, *, method: str = "list_since") -> None:
        self._rows = rows
        self._method = method

    def __getattr__(self, name):  # type: ignore[no-redef]
        if name in ("list_since", "list_events_since", "list_items_since"):

            async def _impl(space_id, since, *, limit=500):
                return [r for r in self._rows if _ts_attr(r) > since][:limit]

            return _impl
        raise AttributeError(name)


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
                _FakeListSinceRepo(gallery_items) if gallery_items is not None else None
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
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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
        posts=[_post(0, datetime(2026, 4, 1, tzinfo=timezone.utc))],
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
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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


async def test_send_request_emits_canonical_payload(provider_factory):
    """``send_request`` posts SPACE_SYNC_RESUME to the right peer."""
    provider, fed, _ = provider_factory(posts=[], members=[])
    await provider.send_request(
        space_id="sp-1",
        instance_id="peer-b",
        since="2026-04-01T00:00:00+00:00",
    )
    assert fed.sent == [
        {
            "to": "peer-b",
            "type": FederationEventType.SPACE_SYNC_RESUME,
            "payload": {
                "space_id": "sp-1",
                "since": "2026-04-01T00:00:00+00:00",
            },
            "space_id": "sp-1",
        }
    ]


async def test_send_request_validates_inputs(provider_factory):
    """Empty fields produce a no-op, not an error."""
    provider, fed, _ = provider_factory(posts=[], members=[])
    await provider.send_request(space_id="", instance_id="peer-b", since="x")
    await provider.send_request(space_id="sp-1", instance_id="", since="x")
    await provider.send_request(space_id="sp-1", instance_id="peer-b", since="")
    assert fed.sent == []


# ── Multi-resource replays ──────────────────────────────────────────


async def test_handle_request_replays_comments(provider_factory):
    """Comments newer than ``since`` go out as SPACE_COMMENT_CREATED."""
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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


async def test_handle_request_replays_pages(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    iso = (base + timedelta(minutes=1)).isoformat()
    pages = [_page(0, iso), _page(1, iso)]
    provider, fed, _ = provider_factory(pages=pages, members=["peer-a"])
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert all(s["type"] == FederationEventType.SPACE_PAGE_CREATED for s in fed.sent)


async def test_handle_request_replays_stickies(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    iso = (base + timedelta(minutes=1)).isoformat()
    stickies = [_sticky(0, iso), _sticky(1, iso)]
    provider, fed, _ = provider_factory(stickies=stickies, members=["peer-a"])
    sent = await provider.handle_request(
        _event("peer-a", {"space_id": "sp-1", "since": base.isoformat()}),
    )
    assert sent == 2
    assert all(s["type"] == FederationEventType.SPACE_STICKY_CREATED for s in fed.sent)


async def test_handle_request_replays_calendar_events(provider_factory):
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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
    # §S-9 thumbnail-only projection — no full ``url`` field.
    assert "url" not in payload
    assert {"id", "album_id", "uploaded_by", "thumbnail_url"} <= set(payload)


async def test_handle_request_aggregates_across_resources(provider_factory):
    """The total return value sums every resource type."""
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
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


# ── _post_to_payload location handling ─────────────────────────────────


def test_post_to_payload_omits_location_when_unset():
    """A normal post (no location) doesn't pollute the wire payload."""
    post = Post(
        id="p-1",
        author="alice",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
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
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
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
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        location=LocationData(lat=10.0, lon=20.0),
    )
    payload = _post_to_payload(post)
    assert payload["location"] == {"lat": 10.0, "lon": 20.0}

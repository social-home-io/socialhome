"""Tests for socialhome.federation.sync.space.exporters.posts_deleted."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from socialhome.domain.post import Post, PostType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
    ROSTER_RESOURCES,
)
from socialhome.federation.sync.space.exporters import (
    PostsDeletedExporter,
    PostsExporter,
)
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE, SyncWindows

_AT = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
_SPACE = Space(
    id="sp-1",
    name="S",
    owner_instance_id="host",
    owner_username="anna",
    identity_public_key="ab" * 32,
    config_sequence=0,
    features=SpaceFeatures(),
    space_type=SpaceType.PRIVATE,
    join_mode=JoinMode.INVITE_ONLY,
)


class _Spaces:
    def __init__(self, space: Space | None) -> None:
        self.space = space

    async def get(self, space_id):
        return self.space


class _Posts:
    """``n`` deleted posts (and one live one), served page by page."""

    def __init__(self, n: int = 1) -> None:
        self.gone = [
            Post(
                id=f"p-{i}",
                author="u-a",
                type=PostType.TEXT,
                created_at=_AT,
                content=None,
                deleted=True,
            )
            for i in range(n)
        ]
        self.live = [
            Post(
                id="p-live",
                author="u-a",
                type=PostType.TEXT,
                created_at=_AT,
                content="secret words",
            )
        ]
        self.asked: list[tuple] = []

    async def list_sync_page(
        self, space_id, *, deleted, cutoff, exempt_types, cursor, limit
    ):
        self.asked.append((deleted, cutoff, exempt_types, cursor, limit))
        rows = self.gone if deleted else self.live
        start = cursor or 0
        page = rows[start : start + limit]
        return page, (start + limit if len(page) == limit else None)


async def test_exports_ids_and_author_never_content():
    repo = _Posts()
    exporter = PostsDeletedExporter(repo, SyncWindows(_Spaces(_SPACE)))  # type: ignore[arg-type]
    assert exporter.resource == "posts_deleted"
    records = await exporter.list_records("sp-1")
    assert records == [
        {
            "id": "p-0",
            "post_id": "p-0",
            "author": "u-a",
            "created_at": _AT.isoformat(),
        }
    ]
    # Asked for the deleted rows, every one (no retention, no cap).
    assert repo.asked == [(True, None, (), None, SYNC_PAGE_SIZE)]


async def test_a_retention_window_bounds_the_tombstones_like_the_posts():
    kept = dataclasses.replace(
        _SPACE, retention_days=7, retention_exempt_types=("poll",)
    )
    repo = _Posts()
    windows = SyncWindows(_Spaces(kept))  # type: ignore[arg-type]
    await PostsDeletedExporter(repo, windows).list_records("sp-1")  # type: ignore[arg-type]
    await PostsExporter(repo, windows).list_records("sp-1")  # type: ignore[arg-type]
    (gone_ask, live_ask) = repo.asked
    assert gone_ask[0] is True and live_ask[0] is False
    assert gone_ask[1] is not None and gone_ask[1] == live_ask[1]
    assert gone_ask[2] == live_ask[2] == ("poll",)


async def test_more_tombstones_than_a_page_all_stream_page_by_page():
    repo = _Posts(SYNC_PAGE_SIZE + 5)
    exporter = PostsDeletedExporter(repo, SyncWindows(_Spaces(None)))  # type: ignore[arg-type]
    pages = [p async for p in exporter.iter_batches("sp-1")]
    assert [len(p) for p in pages] == [SYNC_PAGE_SIZE, 5]
    assert [r["id"] for p in pages for r in p] == [p.id for p in repo.gone]


def test_tombstones_stream_before_the_posts():
    order = list(RESOURCE_ORDER)
    assert order.index("posts_deleted") < order.index("posts")


def test_tombstones_are_a_removal_not_roster_or_content():
    assert "posts_deleted" in REMOVAL_RESOURCES
    assert "posts_deleted" not in ROSTER_RESOURCES

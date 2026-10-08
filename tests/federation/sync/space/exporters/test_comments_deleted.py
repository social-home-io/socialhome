"""Tests for socialhome.federation.sync.space.exporters.comments_deleted."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from socialhome.domain.post import Comment, CommentType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
    ROSTER_RESOURCES,
)
from socialhome.federation.sync.space.exporters import (
    CommentsDeletedExporter,
    CommentsExporter,
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


class _Comments:
    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def list_comments_sync_page(
        self, space_id, *, deleted, cutoff, exempt_types, cursor, limit
    ):
        self.asked.append((deleted, cutoff, exempt_types, cursor, limit))
        comment = Comment(
            id="c-gone" if deleted else "c-live",
            post_id="p-1",
            author="u-b",
            type=CommentType.TEXT,
            created_at=_AT,
            content=None if deleted else "kind words",
            deleted=deleted,
        )
        return [comment], None


async def test_exports_ids_post_and_author_never_content():
    repo = _Comments()
    exporter = CommentsDeletedExporter(repo, SyncWindows(_Spaces(_SPACE)))  # type: ignore[arg-type]
    assert exporter.resource == "comments_deleted"
    assert await exporter.list_records("sp-1") == [
        {
            "id": "c-gone",
            "comment_id": "c-gone",
            "post_id": "p-1",
            "author": "u-b",
            "created_at": _AT.isoformat(),
        }
    ]
    assert repo.asked == [(True, None, (), None, SYNC_PAGE_SIZE)]


async def test_live_comments_never_include_a_deleted_one_and_share_the_window():
    kept = dataclasses.replace(_SPACE, retention_days=3)
    repo = _Comments()
    windows = SyncWindows(_Spaces(kept))  # type: ignore[arg-type]
    live = await CommentsExporter(repo, windows).list_records("sp-1")  # type: ignore[arg-type]
    await CommentsDeletedExporter(repo, windows).list_records("sp-1")  # type: ignore[arg-type]
    assert [r["id"] for r in live] == ["c-live"]
    (live_ask, gone_ask) = repo.asked
    assert live_ask[0] is False and gone_ask[0] is True
    assert live_ask[1] is not None and live_ask[1] == gone_ask[1]


def test_tombstones_stream_after_the_posts_and_before_the_comments():
    order = list(RESOURCE_ORDER)
    assert order.index("posts") < order.index("comments_deleted")
    assert order.index("comments_deleted") < order.index("comments")


def test_tombstones_are_a_removal_not_roster_or_content():
    assert "comments_deleted" in REMOVAL_RESOURCES
    assert "comments_deleted" not in ROSTER_RESOURCES

"""Tests for socialhome.federation.sync.space.exporters.posts_deleted."""

from __future__ import annotations

from socialhome.domain.post import PostTombstone
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
    ROSTER_RESOURCES,
)
from socialhome.federation.sync.space.exporters import PostsDeletedExporter
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE

_AT = "2026-06-01T10:00:00+00:00"


class _Posts:
    """``n`` post tombstones, served page by page."""

    def __init__(self, n: int = 1, **kw) -> None:
        self.rows = [
            PostTombstone(
                id=f"p-{i}",
                author="u-a",
                type=kw.get("type", "text"),
                created_at=_AT,
                moderated=kw.get("moderated", False),
                moderated_by=kw.get("moderated_by"),
            )
            for i in range(n)
        ]
        self.asked: list[tuple] = []

    async def list_post_tombstones_page(self, space_id, *, cursor, limit, since=None):
        self.asked.append((space_id, cursor, limit))
        start = cursor or 0
        page = self.rows[start : start + limit]
        return page, (start + limit if len(page) == limit else None)


async def test_exports_ids_author_and_type_never_content():
    repo = _Posts()
    exporter = PostsDeletedExporter(repo)  # type: ignore[arg-type]
    assert exporter.resource == "posts_deleted"
    assert await exporter.list_records("sp-1") == [
        {
            "id": "p-0",
            "post_id": "p-0",
            "author": "u-a",
            "type": "text",
            "created_at": _AT,
            "moderated": False,
        }
    ]
    # Every tombstone: no retention window, no cap.
    assert repo.asked == [("sp-1", None, SYNC_PAGE_SIZE)]


async def test_a_moderator_removal_names_its_moderator():
    repo = _Posts(type="poll", moderated=True, moderated_by="u-mod")
    (record,) = await PostsDeletedExporter(repo).list_records("sp-1")  # type: ignore[arg-type]
    assert record["type"] == "poll"
    assert record["moderated"] is True
    assert record["actor_user_id"] == "u-mod"


async def test_more_tombstones_than_a_page_all_stream_page_by_page():
    repo = _Posts(SYNC_PAGE_SIZE + 5)
    exporter = PostsDeletedExporter(repo)  # type: ignore[arg-type]
    pages = [p async for p in exporter.iter_batches("sp-1")]
    assert [len(p) for p in pages] == [SYNC_PAGE_SIZE, 5]
    assert [r["id"] for p in pages for r in p] == [t.id for t in repo.rows]


def test_tombstones_stream_before_the_posts():
    order = list(RESOURCE_ORDER)
    assert order.index("posts_deleted") < order.index("posts")


def test_tombstones_are_a_removal_not_roster_or_content():
    assert "posts_deleted" in REMOVAL_RESOURCES
    assert "posts_deleted" not in ROSTER_RESOURCES

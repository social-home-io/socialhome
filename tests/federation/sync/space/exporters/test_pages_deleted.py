"""Tests for socialhome.federation.sync.space.exporters.pages_deleted."""

from __future__ import annotations

from socialhome.domain.page import PageTombstone
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
    ROSTER_RESOURCES,
)
from socialhome.federation.sync.space.exporters import PagesDeletedExporter
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE


class _Repo:
    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def list_page_tombstones(
        self, space_id, *, since=None, limit=500, before=None, since_seq=None
    ):
        self.asked.append((space_id, since, limit, before))
        return [
            PageTombstone(
                id="pg1",
                deleted_at="2026-06-01 10:00:00",
                created_by="u-c",
                deleted_by="u-d",
            )
        ]


async def test_exports_each_tombstone_as_the_delete_payload():
    repo = _Repo()
    exporter = PagesDeletedExporter(repo)
    assert exporter.resource == "pages_deleted"
    assert await exporter.list_records("sp-1") == [
        {
            "id": "pg1",
            "page_id": "pg1",
            "space_id": "sp-1",
            "created_by": "u-c",
            "actor_user_id": "u-d",
        }
    ]
    # Every tombstone, page by page — no fixed count cuts the stream.
    assert repo.asked == [("sp-1", None, SYNC_PAGE_SIZE, None)]


def test_tombstones_stream_before_the_pages():
    order = list(RESOURCE_ORDER)
    assert order.index("pages_deleted") < order.index("pages")


def test_tombstones_are_a_removal_not_roster_or_content():
    assert "pages_deleted" in REMOVAL_RESOURCES
    assert "pages_deleted" not in ROSTER_RESOURCES


class _ManyRepo:
    """``n`` page tombstones, newest delete first, keyset-paged."""

    def __init__(self, n: int) -> None:
        self.rows = [
            PageTombstone(
                id=f"pg{i:05d}",
                deleted_at=f"2026-06-01 10:{i // 60 % 60:02d}:{i % 60:02d}",
                created_by="u-c",
                deleted_by="u-d",
            )
            for i in range(n)
        ]
        self.rows.sort(key=lambda t: (t.deleted_at, t.id), reverse=True)
        self.asked: list = []

    async def list_page_tombstones(
        self, space_id, *, since=None, limit=500, before=None, since_seq=None
    ):
        self.asked.append(before)
        rows = [t for t in self.rows if before is None or (t.deleted_at, t.id) < before]
        return rows[:limit]


async def test_more_tombstones_than_a_page_all_stream():
    repo = _ManyRepo(SYNC_PAGE_SIZE * 2 + 3)
    exporter = PagesDeletedExporter(repo)
    pages = [p async for p in exporter.iter_batches("sp-1")]
    assert [len(p) for p in pages] == [SYNC_PAGE_SIZE, SYNC_PAGE_SIZE, 3]
    assert [r["id"] for p in pages for r in p] == [t.id for t in repo.rows]
    last = repo.rows[SYNC_PAGE_SIZE - 1]
    assert repo.asked[:2] == [None, (last.deleted_at, last.id)]

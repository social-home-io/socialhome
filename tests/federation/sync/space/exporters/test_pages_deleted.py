"""Tests for socialhome.federation.sync.space.exporters.pages_deleted."""

from __future__ import annotations

from socialhome.domain.page import PageTombstone
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
    ROSTER_RESOURCES,
)
from socialhome.federation.sync.space.exporters import PagesDeletedExporter
from socialhome.federation.sync.space.exporters.pages_deleted import (
    MAX_TOMBSTONES_STREAMED,
)


class _Repo:
    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def list_page_tombstones(self, space_id, *, since=None, limit=500):
        self.asked.append((space_id, since, limit))
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
    assert repo.asked == [("sp-1", None, MAX_TOMBSTONES_STREAMED)]


def test_tombstones_stream_before_the_pages():
    order = list(RESOURCE_ORDER)
    assert order.index("pages_deleted") < order.index("pages")


def test_tombstones_are_a_removal_not_roster_or_content():
    assert "pages_deleted" in REMOVAL_RESOURCES
    assert "pages_deleted" not in ROSTER_RESOURCES

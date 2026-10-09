"""Tests for socialhome.federation.sync.space.exporters.row_tombstones."""

from __future__ import annotations

from socialhome.domain.tombstone import SpaceRowTombstone
from socialhome.federation.sync.space.exporters.row_tombstones import (
    RowTombstonesExporter,
)
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE


class _Exporter(RowTombstonesExporter):
    resource = "things_deleted"
    owner_key = "made_by"
    parent_key = "box_id"

    __slots__ = ()


def _t(i: int, *, by: str = "") -> SpaceRowTombstone:
    return SpaceRowTombstone(
        id=f"t{i}",
        owner="u-o",
        created_at="2026-01-01",
        deleted_at="2026-02-01 00:00:00",
        deleted_by=by,
        parent_id="box",
    )


async def test_pages_through_the_fetch_until_the_cursor_ends():
    asked: list[tuple] = []
    pages = {None: ([_t(1, by="u-d"), _t(2)], 7), 7: ([_t(3)], None)}

    async def fetch(space_id, *, cursor, limit, since=None):
        asked.append((space_id, cursor, limit))
        return pages[cursor]

    exporter = _Exporter(fetch)
    batches = [b async for b in exporter.iter_batches("sp")]
    assert [len(b) for b in batches] == [2, 1]
    assert asked == [("sp", None, SYNC_PAGE_SIZE), ("sp", 7, SYNC_PAGE_SIZE)]
    first, second = batches[0]
    # Identity only: the owner under the live key, the parent, the actor
    # only when one was recorded — never content.
    assert first == {
        "id": "t1",
        "made_by": "u-o",
        "created_at": "2026-01-01",
        "box_id": "box",
        "actor_user_id": "u-d",
    }
    assert "actor_user_id" not in second


async def test_no_parent_key_no_parent_field():
    class _Flat(RowTombstonesExporter):
        resource = "flat_deleted"
        owner_key = "author"
        __slots__ = ()

    async def fetch(space_id, *, cursor, limit, since=None):
        return [_t(1)], None

    assert await _Flat(fetch).list_records("sp") == [
        {"id": "t1", "author": "u-o", "created_at": "2026-01-01"}
    ]

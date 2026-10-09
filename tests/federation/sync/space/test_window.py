"""Tests for :mod:`socialhome.federation.sync.space.window`."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.sync.space.window import (
    KEEP_FOREVER,
    SYNC_PAGE_SIZE,
    SyncWindow,
    SyncWindows,
    iter_pages,
    iter_tombstone_pages,
    window_for_space,
)

_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
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


def test_no_retention_keeps_forever():
    assert window_for_space(_SPACE, now=_NOW) == KEEP_FOREVER == SyncWindow()
    assert window_for_space(None, now=_NOW) == KEEP_FOREVER
    zero = dataclasses.replace(_SPACE, retention_days=0)
    assert window_for_space(zero, now=_NOW) == KEEP_FOREVER


def test_retention_sets_the_cutoff_and_the_exempt_types():
    kept = dataclasses.replace(
        _SPACE, retention_days=7, retention_exempt_types=("schedule", "poll")
    )
    window = window_for_space(kept, now=_NOW)
    assert window.cutoff == "2026-10-01 12:00:00"
    assert window.exempt_types == ("poll", "schedule")


async def test_sync_windows_reads_the_space_when_asked():
    class _Spaces:
        def __init__(self) -> None:
            self.space: Space | None = dataclasses.replace(_SPACE, retention_days=30)

        async def get(self, space_id):
            assert space_id == "sp-1"
            return self.space

    spaces = _Spaces()
    windows = SyncWindows(spaces)  # type: ignore[arg-type]
    assert (await windows.for_space("sp-1")).cutoff is not None
    spaces.space = None
    assert await windows.for_space("sp-1") == KEEP_FOREVER


async def test_retention_key_names_the_retention_an_incremental_shape_needs():
    class _Spaces:
        space: Space | None = _SPACE

        async def get(self, space_id):
            return self.space

    spaces = _Spaces()
    windows = SyncWindows(spaces)  # type: ignore[arg-type]
    assert await windows.retention_key("sp-1") == ""
    spaces.space = dataclasses.replace(
        _SPACE, retention_days=30, retention_exempt_types=("schedule", "poll")
    )
    assert await windows.retention_key("sp-1") == "30:poll,schedule"
    spaces.space = dataclasses.replace(_SPACE, retention_days=7)
    assert await windows.retention_key("sp-1") == "7:"


async def test_iter_pages_follows_the_cursor_to_the_end():
    asked: list[int | None] = []

    async def fetch(cursor):
        asked.append(cursor)
        return {None: ([1, 2], 5), 5: ([], 9), 9: ([3], None)}[cursor]

    assert [page async for page in iter_pages(fetch)] == [[1, 2], [3]]
    assert asked == [None, 5, 9]


async def test_iter_tombstone_pages_stops_on_a_short_page():
    rows = [(f"2026-01-01 00:00:{i:02d}", f"t{i}") for i in range(SYNC_PAGE_SIZE + 1)]
    asked: list = []

    async def fetch(before):
        asked.append(before)
        left = [r for r in rows if before is None or r < before]
        return sorted(left, reverse=True)[:SYNC_PAGE_SIZE]

    pages = [p async for p in iter_tombstone_pages(fetch, lambda r: r)]
    assert [len(p) for p in pages] == [SYNC_PAGE_SIZE, 1]
    assert len(asked) == 2

    async def empty(before):
        return []

    assert [p async for p in iter_tombstone_pages(empty, lambda r: r)] == []

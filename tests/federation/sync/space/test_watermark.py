"""Unit tests for :mod:`socialhome.federation.sync.space.watermark` — when a
periodic session may stream only the changed rows, and when it must not."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.space import SpaceSyncWatermark
from socialhome.federation.sync.space.watermark import (
    FULL_RESYNC_INTERVAL_S,
    SYNC_SHAPE_VERSION,
    SyncWatermarks,
    session_shape,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class _Repo:
    def __init__(self, seq: int = 0, wm: SpaceSyncWatermark | None = None) -> None:
        self.seq = seq
        self.wm = wm
        self.confirmed: list[tuple] = []

    async def current_seq(self) -> int:
        return self.seq

    async def get(self, space_id: str, instance_id: str):
        return self.wm

    async def confirm(self, space_id, instance_id, *, seq, shape, full_at):
        self.confirmed.append((space_id, instance_id, seq, shape, full_at))


def _wm(*, seq=7, shape="S", age_s: float = 60.0) -> SpaceSyncWatermark:
    return SpaceSyncWatermark(
        seq=seq, shape=shape, full_at=(NOW - timedelta(seconds=age_s)).isoformat()
    )


def _marks(repo: _Repo) -> SyncWatermarks:
    return SyncWatermarks(repo, clock=lambda: NOW)


def test_the_shape_names_everything_a_stream_depends_on():
    shape = session_shape(
        peer_version=55, retention="30:event", resources=("bans", "posts")
    )
    assert shape == f"v{SYNC_SHAPE_VERSION}|p55|r30:event|bans,posts"
    assert shape != session_shape(
        peer_version=56, retention="30:event", resources=("bans", "posts")
    )
    assert shape != session_shape(
        peer_version=55, retention="", resources=("bans", "posts")
    )
    assert shape != session_shape(
        peer_version=55,
        retention="30:event",
        resources=("bans", "posts", "chat_messages"),
    )


async def test_snapshot_reads_the_counter():
    assert await _marks(_Repo(seq=42)).snapshot() == 42


async def test_an_incremental_session_with_a_fresh_matching_watermark_streams_since_it():
    since = await _marks(_Repo(wm=_wm())).since_for(
        space_id="sp", instance_id="h", shape="S", sync_mode="incremental"
    )
    assert since == 7


@pytest.mark.parametrize("mode", ["initial", "full"])
async def test_every_other_session_streams_in_full(mode):
    since = await _marks(_Repo(wm=_wm())).since_for(
        space_id="sp", instance_id="h", shape="S", sync_mode=mode
    )
    assert since is None


@pytest.mark.parametrize(
    "wm",
    [
        None,  # never confirmed / seat dropped / older requester
        _wm(shape="OLD"),  # upgrade, chat gate, retention change
        _wm(age_s=FULL_RESYNC_INTERVAL_S + 1),  # daily anti-entropy
        SpaceSyncWatermark(seq=7, shape="S", full_at=None),
        SpaceSyncWatermark(seq=7, shape="S", full_at="not a date"),
    ],
)
async def test_fails_safe_toward_a_full_stream(wm):
    since = await _marks(_Repo(wm=wm)).since_for(
        space_id="sp", instance_id="h", shape="S", sync_mode="incremental"
    )
    assert since is None


async def test_a_naive_full_at_reads_as_utc():
    wm = SpaceSyncWatermark(seq=3, shape="S", full_at="2026-10-09 11:00:00")
    since = await _marks(_Repo(wm=wm)).since_for(
        space_id="sp", instance_id="h", shape="S", sync_mode="incremental"
    )
    assert since == 3


async def test_confirm_records_a_full_stream_with_its_time():
    repo = _Repo()
    await _marks(repo).confirm(
        space_id="sp", instance_id="h", seq=9, shape="S", full=True
    )
    assert repo.confirmed == [("sp", "h", 9, "S", NOW.isoformat())]


async def test_confirm_keeps_the_last_full_time_for_an_incremental_stream():
    repo = _Repo()
    await _marks(repo).confirm(
        space_id="sp", instance_id="h", seq=9, shape="S", full=False
    )
    assert repo.confirmed == [("sp", "h", 9, "S", None)]

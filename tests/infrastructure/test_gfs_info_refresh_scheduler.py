"""Tests for :class:`GfsInfoRefreshScheduler` — the periodic, jittered
``/gfs/info`` re-read that lets households converge on a GFS's new id."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.infrastructure import gfs_info_refresh_scheduler as mod
from socialhome.infrastructure.gfs_info_refresh_scheduler import (
    GfsInfoRefreshScheduler,
)


def test_the_delay_is_jittered_around_the_interval():
    s = GfsInfoRefreshScheduler(
        lambda **_: asyncio.sleep(0), interval_s=100, jitter_s=20, rand=lambda: 0.0
    )
    assert s.next_delay() == 80
    s = GfsInfoRefreshScheduler(
        lambda **_: asyncio.sleep(0), interval_s=100, jitter_s=20, rand=lambda: 1.0
    )
    assert s.next_delay() == 120


def test_defaults_are_about_hourly():
    assert mod.DEFAULT_INTERVAL_S == 3600
    assert 0 < mod.DEFAULT_JITTER_S < mod.DEFAULT_INTERVAL_S


async def test_it_refreshes_on_every_tick_until_stopped():
    calls: list[int] = []

    async def refresh(*, should_stop) -> int:
        calls.append(1)
        assert not should_stop()
        return 1

    s = GfsInfoRefreshScheduler(refresh, interval_s=0.01, jitter_s=0)
    await s.start()
    await s.start()  # idempotent
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    await s.stop()
    n = len(calls)
    assert n >= 3
    await asyncio.sleep(0.05)
    assert len(calls) == n


async def test_a_failing_refresh_keeps_the_loop_alive(caplog):
    calls: list[int] = []

    async def refresh(*, should_stop) -> int:
        calls.append(1)
        raise RuntimeError("boom")

    s = GfsInfoRefreshScheduler(refresh, interval_s=0.01, jitter_s=0)
    with caplog.at_level("WARNING"):
        await s.start()
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        await s.stop()
    assert len(calls) >= 2
    assert any("refresh" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_a_non_positive_interval_is_refused(interval):
    with pytest.raises(ValueError):
        GfsInfoRefreshScheduler(lambda **_: asyncio.sleep(0), interval_s=interval)

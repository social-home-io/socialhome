"""Tests for ModerationExpiryScheduler (§4.3 moderation queue sweep)."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.moderation_expiry_scheduler import (
    ModerationExpiryScheduler,
)


class _FakeSweeper:
    def __init__(self, *, fail: bool = False) -> None:
        self.expired = 0
        self.purged = 0
        self.fail = fail

    async def expire_due(self) -> int:
        self.expired += 1
        if self.fail:
            raise RuntimeError("db gone")
        return 2

    async def purge_decided(self) -> int:
        self.purged += 1
        return 1


async def test_run_once_expires_then_purges():
    sweeper = _FakeSweeper()
    sched = ModerationExpiryScheduler(sweeper)
    assert await sched.run_once() == (2, 1)
    assert (sweeper.expired, sweeper.purged) == (1, 1)


async def test_loop_runs_immediately_and_stops_on_event():
    sweeper = _FakeSweeper()
    sched = ModerationExpiryScheduler(sweeper, interval_seconds=3600)
    await sched.start()
    for _ in range(50):
        if sweeper.expired:
            break
        await asyncio.sleep(0.01)
    assert sweeper.expired == 1
    assert sched._stop.is_set() is False
    await sched.stop()
    assert sched._stop.is_set() is True
    assert sched._task is None


async def test_loop_survives_a_failing_sweep():
    sweeper = _FakeSweeper(fail=True)
    sched = ModerationExpiryScheduler(sweeper, interval_seconds=0.01)
    await sched.start()
    for _ in range(100):
        if sweeper.expired >= 2:
            break
        await asyncio.sleep(0.01)
    await sched.stop()
    assert sweeper.expired >= 2


async def test_double_start_is_idempotent_and_restartable():
    sched = ModerationExpiryScheduler(_FakeSweeper(), interval_seconds=10.0)
    await sched.start()
    task = sched._task
    await sched.start()
    assert sched._task is task
    await sched.stop()
    await sched.start()
    assert sched._stop.is_set() is False
    await sched.stop()


async def test_stop_without_start_is_safe():
    await ModerationExpiryScheduler(_FakeSweeper()).stop()

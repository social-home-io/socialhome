"""Tests for :class:`MomentRetentionScheduler`."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.moment_retention_scheduler import (
    MomentRetentionScheduler,
)


class _Moments:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def expire_due(self) -> int:
        self._calls.append("expire")
        return 1


class _Archive:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def enforce_caps(self) -> int:
        self._calls.append("cap")
        return 2


class _Boom:
    async def expire_due(self) -> int:
        raise RuntimeError("db gone")

    async def enforce_caps(self) -> int:
        raise RuntimeError("disk gone")


async def _one_tick(sched: MomentRetentionScheduler) -> None:
    await sched.start()
    await asyncio.sleep(0.05)
    await sched.stop()


async def test_a_tick_prunes_then_enforces_the_archive_cap():
    calls: list[str] = []
    sched = MomentRetentionScheduler(
        _Moments(calls), archive=_Archive(calls), interval_seconds=3600
    )
    await _one_tick(sched)
    assert calls == ["expire", "cap"]


async def test_without_an_archive_service_only_prunes():
    calls: list[str] = []
    await _one_tick(MomentRetentionScheduler(_Moments(calls), interval_seconds=3600))
    assert calls == ["expire"]


async def test_a_failing_step_never_stops_the_loop():
    boom = _Boom()
    sched = MomentRetentionScheduler(boom, archive=boom, interval_seconds=0.01)
    await sched.start()
    await asyncio.sleep(0.05)
    assert sched._task is not None and not sched._task.done()
    await sched.stop()


async def test_start_and_stop_are_idempotent():
    calls: list[str] = []
    sched = MomentRetentionScheduler(_Moments(calls), interval_seconds=3600)
    await sched.start()
    await sched.start()
    await sched.stop()
    await sched.stop()

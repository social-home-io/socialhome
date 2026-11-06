"""Tests for the media orphan sweep scheduler lifecycle."""

import asyncio

import pytest

from socialhome.infrastructure.media_orphan_sweep_scheduler import (
    MediaOrphanSweepScheduler,
)

pytestmark = pytest.mark.asyncio


class _FakeService:
    def __init__(self):
        self.calls = 0
        self.transcode_calls = 0
        self.partial_calls = 0

    async def sweep_once(self):
        self.calls += 1
        return 0

    async def sweep_transcode_src_once(self):
        self.transcode_calls += 1
        return 0

    async def sweep_partial_once(self):
        self.partial_calls += 1
        return 0


async def test_loop_runs_sweep_then_stops_cleanly():
    svc = _FakeService()
    sched = MediaOrphanSweepScheduler(svc, interval_seconds=0.05)
    await sched.start()
    for _ in range(20):
        if svc.calls:
            break
        await asyncio.sleep(0.05)
    await sched.stop()
    assert svc.calls >= 1
    # Each tick also reaps the transcode_src source stash.
    assert svc.transcode_calls >= 1
    # … and the abandoned chunk sets under ``.partial/``.
    assert svc.partial_calls >= 1


async def test_a_failing_sweep_does_not_kill_the_loop():
    class _Boom(_FakeService):
        async def sweep_once(self):
            self.calls += 1
            raise RuntimeError("boom")

    svc = _Boom()
    sched = MediaOrphanSweepScheduler(svc, interval_seconds=0.05)
    await sched.start()
    for _ in range(20):
        if svc.calls >= 2:  # survived a failure and ran again
            break
        await asyncio.sleep(0.05)
    await sched.stop()
    assert svc.calls >= 2

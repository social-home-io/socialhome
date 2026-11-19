"""Tests for :mod:`socialhome.infrastructure.gfs_cleanup_scheduler`."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.gfs_cleanup_scheduler import GfsCleanupScheduler


async def test_a_pass_runs_every_step_and_survives_a_failing_one():
    ran: list[str] = []

    async def boom() -> int:
        ran.append("boom")
        raise RuntimeError("gfs down")

    async def repin() -> int:
        ran.append("repin")
        return 2

    sched = GfsCleanupScheduler([("boom", boom), ("repin", repin)])
    assert await sched.run_once() == 2
    assert ran == ["boom", "repin"]


async def test_the_loop_waits_one_interval_before_its_first_pass_and_stops():
    """The reconnect heal covers a start; the loop is for what stays owed."""
    ran = asyncio.Event()
    count = 0

    async def step() -> int:
        nonlocal count
        count += 1
        ran.set()
        return 0

    sched = GfsCleanupScheduler([("step", step)], interval_seconds=0.02)
    await sched.start()
    await sched.start()  # idempotent
    assert count == 0
    await asyncio.wait_for(ran.wait(), timeout=2)
    await sched.stop()
    seen = count
    await asyncio.sleep(0.06)
    assert count == seen >= 1


async def test_stop_before_start_is_harmless():
    sched = GfsCleanupScheduler([])
    await sched.stop()
    assert await sched.run_once() == 0

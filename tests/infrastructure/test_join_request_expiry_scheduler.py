"""Tests for JoinRequestExpiryScheduler — pending join requests nobody
answered expire (``expires_at``), freeing their slot under the per-space cap
of requests from the GFS (v_84)."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.join_request_expiry_scheduler import (
    JoinRequestExpiryScheduler,
)


class _FakeSpaces:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def expire_due_join_requests(self) -> int:
        self.calls += 1
        if self.fail:
            raise RuntimeError("db gone")
        return 3


async def test_run_once_expires_what_is_due():
    spaces = _FakeSpaces()
    assert await JoinRequestExpiryScheduler(spaces).run_once() == 3
    assert spaces.calls == 1


async def test_loop_runs_immediately_survives_a_failure_and_stops_on_event():
    spaces = _FakeSpaces(fail=True)
    sched = JoinRequestExpiryScheduler(spaces, interval_seconds=3600)
    await sched.start()
    await sched.start()  # idempotent
    for _ in range(50):
        if spaces.calls:
            break
        await asyncio.sleep(0.01)
    assert spaces.calls == 1
    await sched.stop()
    assert sched._task is None

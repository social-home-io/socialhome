"""Tests for UserStatusExpiryScheduler."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.user_status_expiry_scheduler import (
    UserStatusExpiryScheduler,
)


class _FakeUserService:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def clear_expired_statuses(self) -> int:
        self.calls += 1
        if self.fail:
            raise RuntimeError("db gone")
        return 1


async def test_run_once_delegates_to_service():
    svc = _FakeUserService()
    sched = UserStatusExpiryScheduler(svc)  # type: ignore[arg-type]
    assert await sched.run_once() == 1
    assert svc.calls == 1


async def test_loop_sweeps_repeatedly_and_stops_cleanly():
    svc = _FakeUserService()
    sched = UserStatusExpiryScheduler(svc, interval_seconds=0.01)  # type: ignore[arg-type]
    await sched.start()
    await sched.start()  # idempotent
    for _ in range(100):
        if svc.calls >= 2:
            break
        await asyncio.sleep(0.01)
    await sched.stop()
    calls = svc.calls
    assert calls >= 2
    await asyncio.sleep(0.03)
    assert svc.calls == calls  # nothing runs after stop()


async def test_loop_survives_a_failing_sweep():
    svc = _FakeUserService(fail=True)
    sched = UserStatusExpiryScheduler(svc, interval_seconds=0.01)  # type: ignore[arg-type]
    await sched.start()
    for _ in range(100):
        if svc.calls >= 2:
            break
        await asyncio.sleep(0.01)
    await sched.stop()
    assert svc.calls >= 2


async def test_stop_before_start_is_a_noop():
    sched = UserStatusExpiryScheduler(_FakeUserService())  # type: ignore[arg-type]
    await sched.stop()

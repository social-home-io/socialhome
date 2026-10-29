"""Tests for AppUpdateScheduler — daily background update-check loop."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.services.app_update_scheduler import AppUpdateScheduler


class _FakeAppService:
    """Fake AppService that counts check_for_updates calls."""

    def __init__(self, updates: list[dict] | None = None) -> None:
        self._updates = updates or []
        self.call_count: int = 0

    async def check_for_updates(self) -> list[dict]:
        self.call_count += 1
        return list(self._updates)


class _FailingAppService:
    """Fake AppService whose check always raises."""

    def __init__(self) -> None:
        self.call_count = 0

    async def check_for_updates(self) -> list[dict]:
        self.call_count += 1
        raise RuntimeError("catalog unavailable")


@pytest.mark.asyncio
async def test_start_runs_the_update_check():
    """start() runs the check (which announces updates) at once."""
    svc = _FakeAppService([{"app_id": "chess"}])
    scheduler = AppUpdateScheduler(svc, interval=0.05)  # type: ignore[arg-type]

    task = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.12)
    await scheduler.stop()
    await task

    assert svc.call_count >= 1


@pytest.mark.asyncio
async def test_stop_ends_loop_promptly():
    """stop() causes the loop to exit without waiting for the full interval."""
    svc = _FakeAppService()
    scheduler = AppUpdateScheduler(svc, interval=60.0)  # very long interval

    task = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.05)  # let it tick once
    await scheduler.stop()
    # Task should finish promptly — if it hangs, the test times out
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_exception_in_the_check_does_not_crash_loop():
    """If the check raises, the loop continues (logs a warning, doesn't exit)."""
    svc = _FailingAppService()
    scheduler = AppUpdateScheduler(svc, interval=0.05)  # type: ignore[arg-type]

    task = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.15)
    await scheduler.stop()
    await task  # must not raise
    assert svc.call_count >= 2


@pytest.mark.asyncio
async def test_double_start_is_idempotent():
    """Calling start() twice doesn't create a second background loop."""
    svc = _FakeAppService()
    scheduler = AppUpdateScheduler(svc, interval=60.0)  # type: ignore[arg-type]

    task1 = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.05)
    # Second start should be a no-op (the task is already running)
    task2 = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.05)
    await scheduler.stop()
    await task1
    await task2


@pytest.mark.asyncio
async def test_stop_without_start_is_safe():
    """stop() before start() must not raise."""
    svc = _FakeAppService()
    scheduler = AppUpdateScheduler(svc, interval=60.0)  # type: ignore[arg-type]
    await scheduler.stop()  # must not raise


@pytest.mark.asyncio
async def test_loop_ticks_multiple_times():
    """With a short interval, the loop ticks more than once."""
    svc = _FakeAppService()
    scheduler = AppUpdateScheduler(svc, interval=0.04)  # type: ignore[arg-type]

    task = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.20)
    await scheduler.stop()
    await task

    assert svc.call_count >= 2

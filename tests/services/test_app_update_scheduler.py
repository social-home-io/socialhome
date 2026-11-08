"""Tests for AppUpdateScheduler — daily background update-check loop."""

from __future__ import annotations

import asyncio
import logging

import pytest
from aiohttp import web

from socialhome.services.app_catalog_service import AppCatalogService, release_session
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


class _CatalogAppService:
    """Fake AppService whose check fetches a real catalog, as
    :meth:`AppService.check_for_updates` does via ``list_updates(force=True)``."""

    def __init__(self, catalog: AppCatalogService) -> None:
        self._catalog = catalog
        self.call_count = 0

    async def check_for_updates(self) -> list[dict]:
        self.call_count += 1
        await self._catalog.fetch_catalog(force=True)
        return []


@pytest.fixture
async def gateway_timeout_server(aiohttp_server):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=504, reason="Gateway Time-out")

    app = web.Application()
    app.router.add_get("/catalog.json", handler)
    return await aiohttp_server(app)


@pytest.mark.asyncio
async def test_a_504_from_the_catalog_is_one_warning_without_traceback(
    gateway_timeout_server, caplog
):
    """Regression: a slow GitHub (504 on catalog.json, nothing held yet) gave
    a WARNING plus a second record carrying a traceback from the scheduler —
    the federation-demo log audit failed on it. A transient outside failure
    is one WARNING naming the URL and status, and the loop keeps going."""
    url = str(gateway_timeout_server.make_url("/catalog.json"))
    svc = _CatalogAppService(
        AppCatalogService(session_factory=release_session, catalog_url=url)
    )
    scheduler = AppUpdateScheduler(svc, interval=60.0)  # type: ignore[arg-type]

    with caplog.at_level(logging.DEBUG, logger="socialhome"):
        await scheduler.start()
        for _ in range(100):
            if svc.call_count and any(
                r.levelno >= logging.WARNING for r in caplog.records
            ):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert scheduler._task is not None and not scheduler._task.done()
        await scheduler.stop()

    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1, [r.getMessage() for r in loud]
    assert loud[0].levelno == logging.WARNING
    assert loud[0].exc_info is None
    assert url in loud[0].getMessage()
    assert "504" in loud[0].getMessage()


@pytest.mark.asyncio
async def test_an_unexpected_error_still_logs_a_traceback(caplog):
    """A real bug in the check stays visible: logged with its traceback."""
    svc = _FailingAppService()
    scheduler = AppUpdateScheduler(svc, interval=60.0)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await scheduler.start()
        for _ in range(100):
            if caplog.records:
                break
            await asyncio.sleep(0.01)
        await scheduler.stop()

    assert [r.exc_info is not None for r in caplog.records] == [True]
    assert caplog.records[0].levelno == logging.ERROR

"""Tests for :class:`GfsCapabilityWarmup` — the startup ``/gfs/info`` pass."""

from __future__ import annotations

import asyncio
import logging

from socialhome.infrastructure.gfs_capability_warmup import GfsCapabilityWarmup


async def test_start_returns_before_the_pass_finishes_and_the_pass_runs_once():
    release = asyncio.Event()
    calls: list[bool] = []

    async def _warm(*, should_stop):
        calls.append(should_stop())
        await release.wait()
        return 1

    warmup = GfsCapabilityWarmup(_warm)
    await asyncio.wait_for(warmup.start(), timeout=1.0)  # never blocks startup
    await warmup.start()  # idempotent while running
    await asyncio.sleep(0)
    assert calls == [False]

    release.set()
    await warmup.stop()
    assert calls == [False]


async def test_stop_asks_the_pass_to_stop_between_fetches():
    seen_stop: list[bool] = []
    entered = asyncio.Event()

    async def _warm(*, should_stop):
        entered.set()
        while not should_stop():
            await asyncio.sleep(0.01)
        seen_stop.append(True)
        return 0

    warmup = GfsCapabilityWarmup(_warm)
    await warmup.start()
    await entered.wait()
    await warmup.stop()

    assert seen_stop == [True]


async def test_a_failing_pass_is_logged_and_never_raises(caplog):
    async def _warm(*, should_stop):
        raise RuntimeError("no session")

    warmup = GfsCapabilityWarmup(_warm)
    with caplog.at_level(logging.WARNING):
        await warmup.start()
        await warmup.stop()

    assert "capability warm-up failed" in caplog.text


async def test_a_pass_that_ignores_stop_is_cancelled_after_the_grace(monkeypatch):
    async def _warm(*, should_stop):
        await asyncio.sleep(3600)
        return 0

    real_wait_for = asyncio.wait_for

    async def _short_wait_for(aw, timeout):
        return await real_wait_for(aw, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", _short_wait_for)
    warmup = GfsCapabilityWarmup(_warm)
    await warmup.start()
    task = warmup._task
    await warmup.stop()
    await asyncio.sleep(0)

    assert task is not None and task.cancelled()


async def test_stop_without_start_is_a_no_op():
    async def _warm(*, should_stop):  # pragma: no cover — never started
        return 0

    await GfsCapabilityWarmup(_warm).stop()

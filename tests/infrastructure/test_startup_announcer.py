"""Tests for :class:`StartupAnnouncer` — peer-facing startup steps run in
the background so a silent peer never holds the listen port shut."""

from __future__ import annotations

import asyncio
import logging

import pytest

from socialhome.infrastructure import startup_announcer as mod
from socialhome.infrastructure.startup_announcer import StartupAnnouncer


async def _finish(announcer: StartupAnnouncer) -> None:
    """Let every step run, then stop (``stop`` alone skips unstarted steps)."""
    assert announcer._task is not None
    await asyncio.wait_for(asyncio.shield(announcer._task), timeout=1.0)
    await announcer.stop()


async def test_start_returns_while_a_peer_never_answers():
    never = asyncio.Event()
    entered = asyncio.Event()

    async def _silent_peer():
        entered.set()
        await never.wait()

    announcer = StartupAnnouncer([("capabilities", _silent_peer)])
    await asyncio.wait_for(announcer.start(), timeout=1.0)  # never blocks startup
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await announcer.start()  # idempotent while running

    never.set()
    await announcer.stop()


async def test_steps_run_once_in_order():
    calls: list[str] = []

    def _step(name: str):
        async def _run():
            calls.append(name)

        return _run

    announcer = StartupAnnouncer([("a", _step("a")), ("b", _step("b"))])
    await announcer.start()
    await _finish(announcer)
    assert calls == ["a", "b"]


async def test_a_failing_step_is_logged_and_the_next_still_runs(caplog):
    calls: list[str] = []

    async def _boom():
        raise RuntimeError("peer gone")

    async def _next():
        calls.append("next")

    announcer = StartupAnnouncer([("reconnect", _boom), ("capabilities", _next)])
    with caplog.at_level(logging.WARNING):
        await announcer.start()
        await _finish(announcer)

    assert "startup: reconnect failed" in caplog.text
    assert calls == ["next"]


async def test_stop_lets_the_step_in_flight_finish_and_skips_the_rest():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _first():
        entered.set()
        await release.wait()
        calls.append("first")

    async def _second():
        calls.append("second")

    announcer = StartupAnnouncer([("first", _first), ("second", _second)])
    await announcer.start()
    await entered.wait()
    stopping = asyncio.create_task(announcer.stop())
    await asyncio.sleep(0)
    release.set()
    await stopping

    assert calls == ["first"]


async def test_stop_cancels_a_step_that_outlives_the_timeout(monkeypatch):
    monkeypatch.setattr(mod, "STOP_TIMEOUT_S", 0.05)
    entered = asyncio.Event()
    cancelled: list[bool] = []

    async def _silent_peer():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    announcer = StartupAnnouncer([("capabilities", _silent_peer)])
    await announcer.start()
    await entered.wait()
    await asyncio.wait_for(announcer.stop(), timeout=1.0)

    assert cancelled == [True]


@pytest.mark.parametrize("steps", [[]])
async def test_stop_without_start_is_a_noop(steps):
    await StartupAnnouncer(steps).stop()


async def test_wait_returns_once_every_step_ran():
    """A caller (a test seeding peers, say) can wait for the steps without
    knowing the task's name; before ``start`` and after the end it returns
    at once."""
    ran: list[str] = []
    gate = asyncio.Event()

    async def slow() -> None:
        await gate.wait()
        ran.append("slow")

    announcer = StartupAnnouncer([("slow", slow)])
    await announcer.wait()  # not started: nothing to wait for
    await announcer.start()
    waiter = asyncio.create_task(announcer.wait())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    gate.set()
    await asyncio.wait_for(waiter, 1)
    assert ran == ["slow"]
    await announcer.wait()  # done: returns at once

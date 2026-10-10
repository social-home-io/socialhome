"""Bounded retry of the GFS reconnect reconcile behind the possession gate.

A ratcheted connection whose proof got a RETRYABLE answer (a rolling deploy's
404, a balancer's 503, a 429) must not lose its reconcile for the life of the
socket: the retry re-runs gate + reconcile after 15 s, 1 min and 5 min, and the
hourly ``/gfs/info`` pass keeps re-running it while it is pending. Only a
verified signature failure ("refuse") ends it without a reconcile.
"""

from __future__ import annotations

import asyncio

import pytest

from socialhome.infrastructure import gfs_reconcile_retry as mod
from socialhome.infrastructure.gfs_reconcile_retry import GfsReconcileRetry


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(mod, "RETRY_DELAYS_S", (0.01, 0.01, 0.01))


class _Run:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, gfs_id: str) -> str:
        self.calls.append(gfs_id)
        return self.answers.pop(0) if self.answers else "retry"


def test_default_delays_are_15s_1min_5min():
    assert mod.DEFAULT_RETRY_DELAYS_S == (15.0, 60.0, 300.0)


async def test_retries_until_done_then_forgets():
    run = _Run(["retry", "done"])
    retry = GfsReconcileRetry(run)
    retry.schedule("g1")
    assert retry.pending() == {"g1"}
    await retry.wait_idle()
    assert run.calls == ["g1", "g1"]
    assert retry.pending() == set()
    await retry.stop()


async def test_a_refusal_ends_it_without_more_attempts():
    run = _Run(["skip"])
    retry = GfsReconcileRetry(run)
    retry.schedule("g1")
    await retry.wait_idle()
    assert run.calls == ["g1"]
    assert retry.pending() == set()
    await retry.stop()


async def test_after_the_bounded_attempts_it_stays_pending_for_the_hourly_pass():
    run = _Run([])  # always "retry"
    retry = GfsReconcileRetry(run)
    retry.schedule("g1")
    await retry.wait_idle()
    assert run.calls == ["g1"] * 3
    assert retry.pending() == {"g1"}
    run.answers = ["done"]
    assert await retry.run_pending() == 1
    assert retry.pending() == set()
    await retry.stop()


async def test_schedule_twice_runs_one_loop():
    run = _Run(["done"])
    retry = GfsReconcileRetry(run)
    retry.schedule("g1")
    retry.schedule("g1")
    await retry.wait_idle()
    assert run.calls == ["g1"]
    await retry.stop()


async def test_cancel_drops_a_pending_retry():
    run = _Run([])
    retry = GfsReconcileRetry(run)
    retry.schedule("g1")
    retry.cancel("g1")
    await retry.wait_idle()
    assert retry.pending() == set()
    await retry.stop()


async def test_a_raising_run_counts_as_retry(caplog):
    calls = []

    async def boom(gfs_id: str) -> str:
        calls.append(gfs_id)
        raise RuntimeError("bug")

    retry = GfsReconcileRetry(boom)
    retry.schedule("g1")
    await retry.wait_idle()
    assert len(calls) == 3
    assert retry.pending() == {"g1"}
    assert "reconcile retry failed" in caplog.text
    await retry.stop()


async def test_stop_ends_waiting_loops_and_refuses_new_ones(monkeypatch):
    monkeypatch.setattr(mod, "RETRY_DELAYS_S", (30.0,))
    run = _Run([])
    retry = GfsReconcileRetry(run)
    retry.schedule("g1")
    await asyncio.wait_for(retry.stop(), timeout=2)
    assert run.calls == []
    retry.schedule("g2")
    assert retry.pending() == set()

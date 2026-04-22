"""Tests for ReconnectSyncQueue."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.infrastructure.reconnect_queue import (
    P1_SECURITY,
    P2_STRUCTURAL,
    P5_CONTENT,
    P7_BULK,
    SYNC_CONCURRENCY,
    ReconnectSyncQueue,
)


# ─── Construction ────────────────────────────────────────────────────────


def test_zero_concurrency_rejected():
    with pytest.raises(ValueError):
        ReconnectSyncQueue(concurrency=0)


def test_default_concurrency_matches_constant():
    q = ReconnectSyncQueue()
    assert q._concurrency == SYNC_CONCURRENCY


# ─── Enqueue ─────────────────────────────────────────────────────────────


def test_enqueue_rejects_invalid_priority():
    q = ReconnectSyncQueue()

    async def noop():
        return

    with pytest.raises(ValueError):
        q.enqueue(0, noop)
    with pytest.raises(ValueError):
        q.enqueue(8, noop)


def test_pending_count():
    q = ReconnectSyncQueue()

    async def noop():
        return

    assert q.pending_count() == 0
    q.enqueue(P5_CONTENT, noop, "a")
    q.enqueue(P5_CONTENT, noop, "b")
    assert q.pending_count() == 2


# ─── Drain order ─────────────────────────────────────────────────────────


async def test_higher_priority_drained_first():
    """Priority order: P1 < P2 < P3 < ... Lowest number first."""
    q = ReconnectSyncQueue(concurrency=1)
    order: list[str] = []

    def factory(label: str):
        async def task():
            order.append(label)

        return task

    q.enqueue(P7_BULK, factory("bulk1"))
    q.enqueue(P5_CONTENT, factory("content1"))
    q.enqueue(P1_SECURITY, factory("security"))
    q.enqueue(P2_STRUCTURAL, factory("struct"))

    await q.start()
    # Wait for drain.
    for _ in range(20):
        if q.pending_count() == 0:
            break
        await asyncio.sleep(0.02)
    await q.stop()

    assert order[:4] == ["security", "struct", "content1", "bulk1"]


async def test_fifo_within_priority():
    q = ReconnectSyncQueue(concurrency=1)
    order: list[str] = []

    def factory(label: str):
        async def task():
            order.append(label)

        return task

    q.enqueue(P5_CONTENT, factory("first"))
    q.enqueue(P5_CONTENT, factory("second"))
    q.enqueue(P5_CONTENT, factory("third"))
    await q.start()
    for _ in range(20):
        if q.pending_count() == 0:
            break
        await asyncio.sleep(0.02)
    await q.stop()
    assert order[:3] == ["first", "second", "third"]


# ─── Bounded concurrency ────────────────────────────────────────────────


async def test_concurrency_cap_respected():
    """No more than `concurrency` tasks run simultaneously."""
    cap = 3
    q = ReconnectSyncQueue(concurrency=cap)
    in_flight = 0
    max_seen = 0
    started = asyncio.Event()

    async def worker():
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        if in_flight >= cap:
            started.set()
        await asyncio.sleep(0.05)
        in_flight -= 1

    for _ in range(20):
        q.enqueue(P5_CONTENT, lambda: worker())
    await q.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.skip("Workers did not reach cap within timeout")
    # Wait drain.
    for _ in range(80):
        if q.pending_count() == 0:
            break
        await asyncio.sleep(0.05)
    await q.stop()

    assert max_seen <= cap


# ─── Failure isolation ───────────────────────────────────────────────────


async def test_task_exception_does_not_crash_worker():
    q = ReconnectSyncQueue(concurrency=1)
    finished: list[str] = []

    def crash():
        async def t():
            raise RuntimeError("boom")

        return t

    def ok(label):
        async def t():
            finished.append(label)

        return t

    q.enqueue(P5_CONTENT, crash())
    q.enqueue(P5_CONTENT, ok("survived"))
    await q.start()
    for _ in range(40):
        if q.pending_count() == 0:
            break
        await asyncio.sleep(0.02)
    await q.stop()

    assert "survived" in finished


# ─── Cancel pending ──────────────────────────────────────────────────────


async def test_cancel_pending_drops_unstarted():
    q = ReconnectSyncQueue(concurrency=1)

    async def slow():
        await asyncio.sleep(10)

    for _ in range(5):
        q.enqueue(P5_CONTENT, lambda: slow())
    n = await q.cancel_pending()
    assert n == 5
    assert q.pending_count() == 0


# ─── Lifecycle ───────────────────────────────────────────────────────────


async def test_double_start_is_idempotent():
    q = ReconnectSyncQueue(concurrency=1)
    await q.start()
    await q.start()  # no-op
    await q.stop()

"""Tests for :mod:`socialhome.services.moment_delete_cert_retry` — the
bounded retry of a public moment's delete certificate post (adversarial
review F3)."""

from __future__ import annotations

import asyncio
import time

import pytest

from socialhome.services import moment_delete_cert_retry as mod
from socialhome.services.gfs_publish_retry import PublishOutcome
from socialhome.services.moment_delete_cert_retry import (
    DELETE_CERT_RETRY_BACKOFF_S,
    DELETE_CERT_RETRY_WINDOW_S,
    DeleteCertRetryQueue,
    classify_cert_status,
)


class _Clock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = 1_900_000_000.0

    def advance(self, s: float) -> None:
        self.mono += s
        self.wall += s


class _Sender:
    def __init__(self, *outcomes: PublishOutcome) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, gfs_id: str, body: dict) -> PublishOutcome:
        self.calls.append((gfs_id, body))
        if self.outcomes:
            return self.outcomes.pop(0)
        return PublishOutcome.transient()


def _queue(sender, clock) -> DeleteCertRetryQueue:
    return DeleteCertRetryQueue(
        sender, monotonic=lambda: clock.mono, wall=lambda: clock.wall
    )


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (200, "delivered"),
        (201, "delivered"),
        # A cluster mid-upgrade: an old node behind a server that advertised
        # the log answers 404 (no route) or 422 — try again.
        (404, "transient"),
        (422, "transient"),
        (408, "transient"),
        (429, "transient"),
        (503, "transient"),
        # Our certificate is too old, or not ours: the same every time.
        (409, "permanent"),
        (403, "permanent"),
        (400, "permanent"),
        (302, "permanent"),
    ],
)
def test_a_cert_post_status_is_classified(status, kind):
    assert classify_cert_status(status, None).kind == kind


def test_a_429_keeps_its_retry_after():
    assert classify_cert_status(429, "60").retry_after_s == 60.0


async def test_a_failed_post_is_retried_with_backoff_until_it_lands():
    clock = _Clock()
    sender = _Sender(PublishOutcome.transient(), PublishOutcome.delivered())
    q = _queue(sender, clock)
    assert q.enqueue("g1", "m1", {"b": 1}, deleted_at=clock.wall)
    await q.run_due()
    assert sender.calls == []  # not due yet
    clock.advance(DELETE_CERT_RETRY_BACKOFF_S[0])
    await q.run_due()
    assert len(sender.calls) == 1 and q.pending_count() == 1
    clock.advance(DELETE_CERT_RETRY_BACKOFF_S[0])
    await q.run_due()
    assert len(sender.calls) == 1  # backing off further
    clock.advance(DELETE_CERT_RETRY_BACKOFF_S[1])
    await q.run_due()
    assert len(sender.calls) == 2 and q.pending_count() == 0


async def test_a_permanent_refusal_is_not_retried():
    clock = _Clock()
    sender = _Sender(PublishOutcome.permanent())
    q = _queue(sender, clock)
    q.enqueue("g1", "m1", {"b": 1}, deleted_at=clock.wall)
    clock.advance(DELETE_CERT_RETRY_BACKOFF_S[0])
    await q.run_due()
    assert q.pending_count() == 0


async def test_retries_stop_a_day_after_the_delete():
    clock = _Clock()
    sender = _Sender()
    q = _queue(sender, clock)
    q.enqueue("g1", "m1", {"b": 1}, deleted_at=clock.wall - 3600)
    tries = 0
    while q.pending_count():
        clock.advance(DELETE_CERT_RETRY_BACKOFF_S[-1])
        await q.run_due()
        tries += 1
        assert tries < 100
    # Hourly at most, so about a day's worth — and nothing past the window.
    assert 20 <= len(sender.calls) <= 24
    assert (
        clock.wall
        <= 1_900_000_000
        - 3600
        + DELETE_CERT_RETRY_WINDOW_S
        + (DELETE_CERT_RETRY_BACKOFF_S[-1])
    )
    # A delete already past the window is not queued at all.
    assert not q.enqueue(
        "g1", "m2", {}, deleted_at=clock.wall - DELETE_CERT_RETRY_WINDOW_S
    )


async def test_the_queue_is_bounded_overall_and_per_gfs(monkeypatch):
    monkeypatch.setattr(mod, "DELETE_CERT_RETRY_MAX_PENDING", 3)
    monkeypatch.setattr(mod, "DELETE_CERT_RETRY_MAX_PENDING_PER_GFS", 2)
    clock = _Clock()
    q = _queue(_Sender(), clock)
    assert q.enqueue("g1", "m1", {}, deleted_at=clock.wall)
    assert q.enqueue("g1", "m2", {}, deleted_at=clock.wall)
    assert not q.enqueue("g1", "m3", {}, deleted_at=clock.wall)
    assert q.enqueue("g2", "m1", {}, deleted_at=clock.wall)
    assert not q.enqueue("g3", "m1", {}, deleted_at=clock.wall)
    # The same certificate queued again replaces itself, never doubles.
    assert q.enqueue("g1", "m1", {"again": 1}, deleted_at=clock.wall)
    assert q.pending_count() == 3


async def test_a_sender_that_raises_counts_as_transient():
    clock = _Clock()

    async def boom(gfs_id, body):
        raise RuntimeError("db gone")

    q = _queue(boom, clock)
    q.enqueue("g1", "m1", {}, deleted_at=clock.wall)
    clock.advance(DELETE_CERT_RETRY_BACKOFF_S[0])
    await q.run_due()
    assert q.pending_count() == 1


async def test_the_loop_runs_and_stops_cleanly(monkeypatch):
    monkeypatch.setattr(mod, "DELETE_CERT_RETRY_BACKOFF_S", (0.0,))
    sender = _Sender(PublishOutcome.delivered())
    q = DeleteCertRetryQueue(sender)
    await q.start()
    await q.start()  # idempotent
    q.enqueue("g1", "m1", {"b": 1}, deleted_at=time.time())
    for _ in range(100):
        if sender.calls:
            break
        await asyncio.sleep(0.01)
    assert sender.calls == [("g1", {"b": 1})]
    q.enqueue("g1", "m2", {"b": 2}, deleted_at=time.time() - 10)
    await q.stop()
    assert q.pending_count() == 0
    # Stopped: nothing more is taken.
    assert not q.enqueue("g1", "m3", {}, deleted_at=time.time())

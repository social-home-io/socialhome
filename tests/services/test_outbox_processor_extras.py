"""Additional coverage for OutboxProcessor (existing tests cover the basics)."""

from __future__ import annotations


from social_home.domain.federation import FederationEventType
from social_home.infrastructure.outbox_processor import (
    BACKOFF_SECONDS,
    JITTER_RATIO,
    MAX_ATTEMPTS,
    OutboxProcessor,
)
from social_home.repositories.outbox_repo import OutboxEntry


# ─── Constants ───────────────────────────────────────────────────────────


def test_backoff_schedule_monotonic():
    """Backoff intervals must be non-decreasing."""
    for prev, curr in zip(BACKOFF_SECONDS, BACKOFF_SECONDS[1:]):
        assert curr >= prev


def test_backoff_caps_at_4_hours():
    assert BACKOFF_SECONDS[-1] == 14400


def test_max_attempts_matches_schedule_length():
    assert MAX_ATTEMPTS == len(BACKOFF_SECONDS)


def test_jitter_ratio_30_percent():
    assert JITTER_RATIO == 0.30


# ─── Lifecycle ───────────────────────────────────────────────────────────


class _FakeRepo:
    def __init__(self):
        self.pending: list[OutboxEntry] = []
        self.delivered: list[str] = []
        self.failed: list[str] = []
        self.rescheduled: list[tuple[str, str, int]] = []

    async def list_due(self, limit=50):
        return self.pending[:limit]

    async def mark_delivered(self, eid):
        self.delivered.append(eid)
        self.pending = [e for e in self.pending if e.id != eid]

    async def mark_failed(self, eid):
        self.failed.append(eid)
        self.pending = [e for e in self.pending if e.id != eid]

    async def reschedule(self, eid, next_at, attempts):
        self.rescheduled.append((eid, next_at, attempts))
        for i, e in enumerate(self.pending):
            if e.id == eid:
                self.pending[i] = OutboxEntry(
                    id=e.id,
                    instance_id=e.instance_id,
                    event_type=e.event_type,
                    payload_json=e.payload_json,
                    status="pending",
                    attempts=attempts,
                    next_attempt_at=next_at,
                    created_at=e.created_at,
                )


def _entry(eid: str, attempts: int = 0):
    return OutboxEntry(
        id=eid,
        instance_id="i1",
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload_json="{}",
        status="pending",
        attempts=attempts,
        next_attempt_at="2026-04-15T00:00:00+00:00",
        created_at="2026-04-15T00:00:00+00:00",
    )


async def test_double_start_is_idempotent():
    repo = _FakeRepo()

    async def deliver(_):
        return True

    proc = OutboxProcessor(repo, deliver, poll_interval_seconds=0.05)
    await proc.start()
    await proc.start()  # no-op
    await proc.stop()


async def test_stop_without_start_is_safe():
    proc = OutboxProcessor(_FakeRepo(), lambda _: None)
    await proc.stop()  # no raise


async def test_drain_once_returns_zero_when_no_entries():
    proc = OutboxProcessor(_FakeRepo(), lambda _: None)
    assert await proc.drain_once() == 0


async def test_drain_once_marks_delivered():
    repo = _FakeRepo()
    repo.pending.append(_entry("e1"))

    async def deliver(_):
        return True

    proc = OutboxProcessor(repo, deliver)
    n = await proc.drain_once()
    assert n == 1
    assert "e1" in repo.delivered


async def test_drain_once_reschedules_on_failure():
    repo = _FakeRepo()
    repo.pending.append(_entry("e1"))

    async def deliver(_):
        return False

    proc = OutboxProcessor(repo, deliver, rng=lambda: 0.5)
    await proc.drain_once()
    assert repo.rescheduled
    eid, _, attempts = repo.rescheduled[0]
    assert eid == "e1"
    assert attempts == 1


async def test_drain_once_marks_failed_after_max_attempts():
    repo = _FakeRepo()
    repo.pending.append(_entry("e1", attempts=MAX_ATTEMPTS - 1))

    async def deliver(_):
        return False

    proc = OutboxProcessor(repo, deliver)
    await proc.drain_once()
    assert "e1" in repo.failed


async def test_drain_once_treats_exception_as_failure():
    repo = _FakeRepo()
    repo.pending.append(_entry("e1"))

    async def deliver(_):
        raise RuntimeError("boom")

    proc = OutboxProcessor(repo, deliver, rng=lambda: 0.5)
    await proc.drain_once()
    # Was rescheduled, not raised.
    assert repo.rescheduled

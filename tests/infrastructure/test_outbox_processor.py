"""Tests for social_home.infrastructure.outbox_processor — OutboxProcessor."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock


from social_home.infrastructure.outbox_processor import (
    BACKOFF_SECONDS,
    JITTER_RATIO,
    MAX_ATTEMPTS,
    OutboxProcessor,
)
from social_home.repositories.outbox_repo import OutboxEntry
from social_home.domain.federation import FederationEventType


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_entry(attempts: int = 0, entry_id: str = "e1") -> OutboxEntry:
    """Build a minimal OutboxEntry for testing."""
    return OutboxEntry(
        id=entry_id,
        instance_id="inst-abc",
        event_type=FederationEventType.PAIRING_INTRO,
        payload_json="{}",
        status="pending",
        attempts=attempts,
        next_attempt_at=datetime.now(timezone.utc).isoformat(),
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Backoff schedule ──────────────────────────────────────────────────────


def test_backoff_seconds_values():
    """BACKOFF_SECONDS follows the documented schedule."""
    assert BACKOFF_SECONDS[0] == 5
    assert BACKOFF_SECONDS[1] == 10
    assert BACKOFF_SECONDS[-1] == 14400  # 4-hour ceiling


def test_backoff_max_attempts_matches_schedule():
    """MAX_ATTEMPTS equals len(BACKOFF_SECONDS)."""
    assert MAX_ATTEMPTS == len(BACKOFF_SECONDS)


def test_jitter_ratio():
    """JITTER_RATIO is ±30%."""
    assert JITTER_RATIO == 0.30


def test_delay_for_attempt_1():
    """_delay_for(1) produces a value near the base delay of 5 s (±30%)."""
    proc = OutboxProcessor(MagicMock(), AsyncMock(), rng=lambda: 0.5)
    delay = proc._delay_for(1)
    assert (
        BACKOFF_SECONDS[0] * (1 - JITTER_RATIO)
        <= delay
        <= BACKOFF_SECONDS[0] * (1 + JITTER_RATIO)
    )


def test_delay_for_jitter_bounds():
    """_delay_for produces values within ±30% of the base for all attempts."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        proc_low = OutboxProcessor(MagicMock(), AsyncMock(), rng=lambda: 0.0)
        proc_high = OutboxProcessor(MagicMock(), AsyncMock(), rng=lambda: 0.999)
        low = proc_low._delay_for(attempt)
        high = proc_high._delay_for(attempt)
        idx = min(attempt, len(BACKOFF_SECONDS)) - 1
        base = BACKOFF_SECONDS[idx]
        assert low >= max(1.0, base * (1 - JITTER_RATIO) - 0.001)
        assert high <= base * (1 + JITTER_RATIO) + 0.001


def test_delay_for_beyond_max_uses_ceiling():
    """_delay_for with attempt > MAX_ATTEMPTS uses the ceiling base delay."""
    proc = OutboxProcessor(MagicMock(), AsyncMock(), rng=lambda: 0.5)
    delay = proc._delay_for(MAX_ATTEMPTS + 10)
    # Base is the ceiling entry
    assert (
        BACKOFF_SECONDS[-1] * (1 - JITTER_RATIO)
        <= delay
        <= BACKOFF_SECONDS[-1] * (1 + JITTER_RATIO)
    )


# ── drain_once ────────────────────────────────────────────────────────────


async def test_drain_once_empty_repo():
    """drain_once returns 0 when no entries are due."""
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[])
    proc = OutboxProcessor(repo, AsyncMock())
    result = await proc.drain_once()
    assert result == 0


async def test_drain_once_success():
    """drain_once marks an entry delivered when deliver returns True."""
    entry = _make_entry(attempts=0)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.mark_delivered = AsyncMock()

    async def _deliver(e):
        return True

    proc = OutboxProcessor(repo, _deliver)
    result = await proc.drain_once()
    assert result == 1
    repo.mark_delivered.assert_awaited_once_with(entry.id)


async def test_drain_once_failure_reschedules():
    """drain_once reschedules an entry when deliver returns False."""
    entry = _make_entry(attempts=0)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        return False

    proc = OutboxProcessor(repo, _deliver, rng=lambda: 0.5)
    await proc.drain_once()
    repo.reschedule.assert_awaited_once()


async def test_drain_once_max_attempts_marks_failed():
    """When attempts reaches MAX_ATTEMPTS, drain_once marks the entry failed."""
    entry = _make_entry(attempts=MAX_ATTEMPTS - 1)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.mark_failed = AsyncMock()

    async def _deliver(e):
        return False

    proc = OutboxProcessor(repo, _deliver)
    await proc.drain_once()
    repo.mark_failed.assert_awaited_once_with(entry.id)


async def test_drain_once_exception_treated_as_failure():
    """An exception raised by deliver is caught and treated as a retry."""
    entry = _make_entry(attempts=0)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        raise RuntimeError("network error")

    proc = OutboxProcessor(repo, _deliver, rng=lambda: 0.5)
    await proc.drain_once()
    repo.reschedule.assert_awaited_once()


async def test_drain_once_mixed_success_failure():
    """drain_once handles a mix of successful and failing entries correctly."""
    ok_entry = _make_entry(attempts=0, entry_id="ok")
    fail_entry = _make_entry(attempts=0, entry_id="fail")
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[ok_entry, fail_entry])
    repo.mark_delivered = AsyncMock()
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        return e.id == "ok"

    proc = OutboxProcessor(repo, _deliver, rng=lambda: 0.5)
    result = await proc.drain_once()
    assert result == 2
    repo.mark_delivered.assert_awaited_once_with("ok")
    repo.reschedule.assert_awaited_once()


# ── Lifecycle ─────────────────────────────────────────────────────────────


async def test_start_stop_lifecycle():
    """start() creates a background task; stop() cancels it cleanly."""
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[])

    async def _deliver(e):
        return True

    proc = OutboxProcessor(repo, _deliver, poll_interval_seconds=0.01)
    await proc.start()
    assert proc._task is not None
    await proc.stop()
    assert proc._task is None
    assert proc._stop.is_set()


async def test_start_idempotent():
    """Calling start() twice does not create a second task."""
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[])

    proc = OutboxProcessor(repo, AsyncMock(), poll_interval_seconds=0.5)
    await proc.start()
    first_task = proc._task
    await proc.start()
    assert proc._task is first_task
    await proc.stop()

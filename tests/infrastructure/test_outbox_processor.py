"""Tests for socialhome.infrastructure.outbox_processor — OutboxProcessor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock


from socialhome.infrastructure.outbox_processor import (
    BACKOFF_SECONDS,
    JITTER_RATIO,
    MAX_ATTEMPTS,
    DeliveryOutcome,
    OutboxProcessor,
)
from socialhome.repositories.outbox_repo import OutboxEntry
from socialhome.domain.federation import FederationEventType


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
    """drain_once marks an entry delivered on DeliveryOutcome.SUCCESS."""
    entry = _make_entry(attempts=0)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.mark_delivered = AsyncMock()

    async def _deliver(e):
        return DeliveryOutcome.SUCCESS

    proc = OutboxProcessor(repo, _deliver)
    result = await proc.drain_once()
    assert result == 1
    repo.mark_delivered.assert_awaited_once_with(entry.id)


async def test_drain_once_transient_reschedules():
    """drain_once reschedules on DeliveryOutcome.TRANSIENT."""
    entry = _make_entry(attempts=0)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        return DeliveryOutcome.TRANSIENT

    proc = OutboxProcessor(repo, _deliver, rng=lambda: 0.5)
    await proc.drain_once()
    repo.reschedule.assert_awaited_once()


async def test_drain_once_permanent_marks_failed_immediately():
    """drain_once drops the entry on PERMANENT regardless of attempt count.

    A 4xx (replay-cache hit, expired timestamp, banned sender, malformed
    envelope) will never become a 2xx no matter how many times we retry,
    so the processor must short-circuit to ``mark_failed`` without
    consuming a retry budget. Specifically this is the path that drains
    the residual outbox noise left over from the HA-integration charset
    bug (#19) — every retry there now returns 410 ``Replay detected``.
    """
    entry = _make_entry(attempts=0)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.mark_failed = AsyncMock()
    repo.reschedule = AsyncMock()
    repo.mark_delivered = AsyncMock()

    async def _deliver(e):
        return DeliveryOutcome.PERMANENT

    proc = OutboxProcessor(repo, _deliver)
    await proc.drain_once()
    repo.mark_failed.assert_awaited_once_with(entry.id)
    repo.reschedule.assert_not_called()
    repo.mark_delivered.assert_not_called()


async def test_drain_once_permanent_bypasses_never_drop():
    """Even a NEVER_DROP event (e.g. SPACE_MEMBER_BANNED) is dropped on a
    4xx — the receiver returning 410 ``Replay detected`` means it
    already has the event, so the security invariant is satisfied.
    """
    entry = _make_entry(attempts=0)
    # Promote to a NEVER_DROP type by rebuilding the dataclass.
    entry = OutboxEntry(
        id=entry.id,
        instance_id=entry.instance_id,
        event_type=FederationEventType.SPACE_MEMBER_BANNED,
        payload_json=entry.payload_json,
        status=entry.status,
        attempts=entry.attempts,
        next_attempt_at=entry.next_attempt_at,
        created_at=entry.created_at,
    )
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.mark_failed = AsyncMock()

    async def _deliver(e):
        return DeliveryOutcome.PERMANENT

    proc = OutboxProcessor(repo, _deliver)
    await proc.drain_once()
    repo.mark_failed.assert_awaited_once_with(entry.id)


async def test_drain_once_max_attempts_marks_failed():
    """When attempts reaches MAX_ATTEMPTS on TRANSIENT, mark failed."""
    entry = _make_entry(attempts=MAX_ATTEMPTS - 1)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.mark_failed = AsyncMock()

    async def _deliver(e):
        return DeliveryOutcome.TRANSIENT

    proc = OutboxProcessor(repo, _deliver)
    await proc.drain_once()
    repo.mark_failed.assert_awaited_once_with(entry.id)


async def test_drain_once_exception_treated_as_transient():
    """An exception raised by deliver is caught and treated as TRANSIENT."""
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
    """drain_once handles a mix of SUCCESS / TRANSIENT entries correctly."""
    ok_entry = _make_entry(attempts=0, entry_id="ok")
    fail_entry = _make_entry(attempts=0, entry_id="fail")
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[ok_entry, fail_entry])
    repo.mark_delivered = AsyncMock()
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        return DeliveryOutcome.SUCCESS if e.id == "ok" else DeliveryOutcome.TRANSIENT

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
        return DeliveryOutcome.SUCCESS

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


# ── §4.4.7 retention prune ──────────────────────────────────────────────────


async def test_prune_once_calls_expire_past_retention():
    """prune_once delegates to repo.expire_past_retention and returns its count."""
    repo = MagicMock()
    repo.expire_past_retention = AsyncMock(return_value=3)
    repo.purge_terminal = AsyncMock(return_value=0)
    proc = OutboxProcessor(repo, AsyncMock())
    result = await proc.prune_once()
    assert result == 3
    repo.expire_past_retention.assert_awaited_once()
    # The argument is an ISO-8601 'now'.
    (now_iso,), _ = repo.expire_past_retention.await_args
    assert datetime.fromisoformat(now_iso).tzinfo is not None


async def test_prune_once_sums_expire_and_purge():
    """prune_once returns expired + purged, purging terminal rows past grace."""
    repo = MagicMock()
    repo.expire_past_retention = AsyncMock(return_value=2)
    repo.purge_terminal = AsyncMock(return_value=4)
    proc = OutboxProcessor(repo, AsyncMock())
    result = await proc.prune_once()
    assert result == 2 + 4
    repo.purge_terminal.assert_awaited()
    # The purge cutoff is an ISO-8601 timestamp in the past (now - grace).
    (cutoff_iso,), _ = repo.purge_terminal.await_args
    assert datetime.fromisoformat(cutoff_iso).tzinfo is not None


async def test_prune_once_drains_multiple_batches():
    """The batch loop keeps purging until a partial batch signals drained.

    A stub returning [5000, 5000, 1] (full, full, partial) must result in
    three calls and a summed purged count, then stop on the partial batch.
    """
    repo = MagicMock()
    repo.expire_past_retention = AsyncMock(return_value=0)
    repo.purge_terminal = AsyncMock(side_effect=[5000, 5000, 1])
    proc = OutboxProcessor(repo, AsyncMock())
    result = await proc.prune_once()
    assert result == 5000 + 5000 + 1
    assert repo.purge_terminal.await_count == 3


async def test_loop_prunes_on_first_tick_when_last_prune_zero():
    """With _last_prune left at 0.0, the loop prunes on the first tick."""
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[])
    repo.expire_past_retention = AsyncMock(return_value=0)

    repo.purge_terminal = AsyncMock(return_value=0)
    proc = OutboxProcessor(repo, AsyncMock(), poll_interval_seconds=0.01)
    assert proc._last_prune is None  # "never pruned" → first tick prunes
    await proc.start()
    # Give the loop a moment to run a tick.
    for _ in range(50):
        if repo.expire_past_retention.await_count:
            break
        await asyncio.sleep(0.01)
    await proc.stop()
    repo.expire_past_retention.assert_awaited()


async def test_loop_skips_prune_within_interval():
    """A recent _last_prune gates the sweep until the interval elapses."""
    import time

    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[])
    repo.expire_past_retention = AsyncMock(return_value=0)

    proc = OutboxProcessor(
        repo,
        AsyncMock(),
        poll_interval_seconds=0.01,
        prune_interval_seconds=3600.0,
    )
    # Pretend we just pruned — the next several ticks must NOT prune again.
    proc._last_prune = time.monotonic()
    await proc.start()
    await asyncio.sleep(0.1)
    await proc.stop()
    repo.expire_past_retention.assert_not_called()


async def test_prune_once_runs_after_prune_hook_after_the_sweep():
    """The hook sees the post-sweep outbox: expired rows are already failed
    (the unpair tombstone sweep relies on this ordering)."""
    order: list[str] = []
    repo = MagicMock()

    async def _expire(now_iso):
        order.append("expire")
        return 0

    async def _hook():
        order.append("hook")
        return 0

    repo.expire_past_retention = AsyncMock(side_effect=_expire)
    repo.purge_terminal = AsyncMock(return_value=0)
    proc = OutboxProcessor(repo, AsyncMock(), after_prune=_hook)
    await proc.prune_once()
    assert order == ["expire", "hook"]


async def test_prune_once_survives_a_failing_after_prune_hook():
    repo = MagicMock()
    repo.expire_past_retention = AsyncMock(return_value=1)
    repo.purge_terminal = AsyncMock(return_value=0)
    hook = AsyncMock(side_effect=RuntimeError("boom"))
    proc = OutboxProcessor(repo, AsyncMock(), after_prune=hook)
    assert await proc.prune_once() == 1
    hook.assert_awaited_once()

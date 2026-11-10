"""Tests for socialhome.infrastructure.outbox_processor — OutboxProcessor."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_retention import capped_delete_key
from socialhome.domain.gfs_mailbox import MULTI_MAX_RECIPIENTS
from socialhome.infrastructure import outbox_processor as outbox_processor_mod
from socialhome.infrastructure.outbox_processor import (
    BACKOFF_SECONDS,
    JITTER_RATIO,
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_S,
    PARALLEL_RECIPIENTS,
    DeliveryOutcome,
    GatedOutboxMulti,
    OutboxProcessor,
    RetryAfter,
)
from socialhome.repositories.outbox_repo import OutboxEntry

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
        next_attempt_at=datetime.now(UTC).isoformat(),
        created_at=datetime.now(UTC).isoformat(),
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


async def test_one_pass_reads_a_whole_multi_group():
    """A broadcast to 500 mailbox seats is read in ONE pass, so it goes up as
    one upload — not in 50-row chunks over ten passes."""
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[])
    await OutboxProcessor(repo, AsyncMock()).drain_once()
    repo.list_due.assert_awaited_once_with(MULTI_MAX_RECIPIENTS)


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


async def test_wake_runs_a_pass_before_the_poll_interval():
    """A broadcast that queued a multi payload wakes the loop (v_90)."""
    repo = MagicMock()
    passes = asyncio.Event()
    calls = 0

    async def _due(_limit):
        nonlocal calls
        calls += 1
        if calls == 2:
            passes.set()
        return []

    repo.list_due = _due
    repo.purge_orphaned = AsyncMock(return_value=0)
    repo.expire_past_retention = AsyncMock(return_value=0)
    repo.purge_terminal = AsyncMock(return_value=0)
    proc = OutboxProcessor(repo, AsyncMock(), poll_interval_seconds=3600)
    await proc.start()
    await _until(lambda: calls == 1)
    proc.wake()
    await asyncio.wait_for(passes.wait(), timeout=2)
    await proc.stop()
    assert proc._task is None


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
    repo.purge_orphaned = AsyncMock(return_value=0)
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
    repo.purge_orphaned = AsyncMock(return_value=0)
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
    repo.purge_orphaned = AsyncMock(return_value=0)
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

    repo.purge_orphaned = AsyncMock(return_value=0)
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
    repo.purge_orphaned = AsyncMock(return_value=0)
    repo.purge_terminal = AsyncMock(return_value=0)
    proc = OutboxProcessor(repo, AsyncMock(), after_prune=_hook)
    await proc.prune_once()
    assert order == ["expire", "hook"]


async def test_prune_once_survives_a_failing_after_prune_hook():
    repo = MagicMock()
    repo.expire_past_retention = AsyncMock(return_value=1)
    repo.purge_orphaned = AsyncMock(return_value=0)
    repo.purge_terminal = AsyncMock(return_value=0)
    hook = AsyncMock(side_effect=RuntimeError("boom"))
    proc = OutboxProcessor(repo, AsyncMock(), after_prune=hook)
    assert await proc.prune_once() == 1
    hook.assert_awaited_once()


async def test_prune_once_purges_rows_of_households_that_are_gone():
    """The sweep deletes outbox rows whose ``remote_instances`` row is gone
    (a crash mid-purge), and counts them, before the after-prune hook."""
    order: list[str] = []
    repo = MagicMock()
    repo.expire_past_retention = AsyncMock(return_value=0)
    repo.purge_terminal = AsyncMock(return_value=0)

    async def _orphans():
        order.append("orphans")
        return 3

    async def _hook():
        order.append("hook")

    repo.purge_orphaned = AsyncMock(side_effect=_orphans)
    proc = OutboxProcessor(repo, AsyncMock(), after_prune=_hook)
    assert await proc.prune_once() == 3
    assert order == ["orphans", "hook"]


# ── RetryAfter (a 429's Retry-After) ──────────────────────────────────────


def _next_at_delay_s(repo) -> float:
    """Seconds between now and the ``next_at`` the processor rescheduled to."""
    (_entry_id, next_at), _kwargs = repo.reschedule.await_args
    return (datetime.fromisoformat(next_at) - datetime.now(UTC)).total_seconds()


async def test_retry_after_floors_the_backoff_delay():
    """A peer that says "come back in 120 s" is not retried in 5 s: the
    Retry-After is a floor on the ordinary backoff, never a shortcut."""
    entry = _make_entry(attempts=0)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        return RetryAfter(seconds=120.0)

    proc = OutboxProcessor(repo, _deliver, rng=lambda: 0.5)
    await proc.drain_once()

    # rng 0.5 → floor jittered up by 15 %: 138 s, never below the 120 asked.
    assert 136.0 <= _next_at_delay_s(repo) <= 139.0
    assert repo.reschedule.await_args.kwargs["attempts"] == 1


async def test_retry_after_shorter_than_the_backoff_keeps_the_backoff():
    entry = _make_entry(attempts=6)  # 7th attempt: base delay 320 s
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        return RetryAfter(seconds=1.0)

    proc = OutboxProcessor(repo, _deliver, rng=lambda: 0.5)
    await proc.drain_once()

    assert 318.0 <= _next_at_delay_s(repo) <= 321.0


async def test_retry_after_is_capped_so_a_peer_cannot_park_an_entry():
    entry = _make_entry(attempts=0)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        return RetryAfter(seconds=10_000_000.0)

    proc = OutboxProcessor(repo, _deliver, rng=lambda: 0.5)
    await proc.drain_once()

    assert _next_at_delay_s(repo) <= MAX_RETRY_AFTER_S * (1 + JITTER_RATIO) + 1.0


async def test_retry_after_floor_is_jittered_up_never_down():
    """Every envelope one burst throttled must not come back on the same
    tick: the floor is spread upward by up to JITTER_RATIO, never below."""

    async def _deliver(e):
        return RetryAfter(seconds=100.0)

    delays = []
    for sample in (0.0, 0.999):
        repo = MagicMock()
        repo.list_due = AsyncMock(return_value=[_make_entry(attempts=0)])
        repo.reschedule = AsyncMock()
        proc = OutboxProcessor(repo, _deliver, rng=lambda sample=sample: sample)
        await proc.drain_once()
        delays.append(_next_at_delay_s(repo))

    low, high = delays
    assert 98.0 <= low <= 101.0
    assert 128.0 <= high <= 131.0


async def test_retry_after_still_spends_the_attempt_budget():
    """A peer that 429s forever cannot grow the outbox without bound: each
    Retry-After costs an attempt like any transient failure, so an ordinary
    envelope is given up on at MAX_ATTEMPTS (the 7-day TTL and the per-peer
    pending cap bound it on top)."""
    entry = _make_entry(attempts=MAX_ATTEMPTS - 1)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.mark_failed = AsyncMock()
    repo.reschedule = AsyncMock()

    async def _deliver(e):
        return RetryAfter(seconds=30.0)

    proc = OutboxProcessor(repo, _deliver)
    await proc.drain_once()

    repo.mark_failed.assert_awaited_once_with(entry.id)
    repo.reschedule.assert_not_called()


async def test_a_moment_delete_keeps_retrying_past_max_attempts():
    """v_66: a MOMENT_DELETED (LONG_LIVED) is pinned on the ceiling backoff
    past MAX_ATTEMPTS — not marked failed — so a household offline for days
    still gets it; the 30-day ``expires_at`` (retention sweep) ends it."""
    entry = OutboxEntry(
        id="del-1",
        instance_id="inst-abc",
        event_type=FederationEventType.MOMENT_DELETED,
        payload_json="{}",
        status="pending",
        attempts=MAX_ATTEMPTS + 40,
        next_attempt_at=datetime.now(UTC).isoformat(),
        created_at=datetime.now(UTC).isoformat(),
    )
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.reschedule = AsyncMock()
    repo.mark_failed = AsyncMock()
    proc = OutboxProcessor(
        repo, AsyncMock(return_value=DeliveryOutcome.TRANSIENT), rng=lambda: 0.5
    )
    await proc.drain_once()
    repo.mark_failed.assert_not_called()
    (_eid, next_at), kw = repo.reschedule.await_args
    assert kw["attempts"] == MAX_ATTEMPTS + 41
    delay = (datetime.fromisoformat(next_at) - datetime.now(UTC)).total_seconds()
    assert abs(delay - BACKOFF_SECONDS[-1]) < 60


async def test_an_ordinary_event_still_gives_up_at_max_attempts():
    entry = _make_entry(attempts=MAX_ATTEMPTS - 1)
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.mark_failed = AsyncMock()
    repo.reschedule = AsyncMock()
    proc = OutboxProcessor(repo, AsyncMock(return_value=DeliveryOutcome.TRANSIENT))
    await proc.drain_once()
    repo.mark_failed.assert_awaited_once_with(entry.id)


async def test_a_capped_relayed_delete_gives_up_like_an_ordinary_event():
    """Adversarial review F2: a relayed delete past its origin's long-lived
    cap is queued in the ordinary class — it gives up at MAX_ATTEMPTS."""
    entry = OutboxEntry(
        id="del-2",
        instance_id="inst-abc",
        event_type=FederationEventType.MOMENT_DELETED,
        payload_json="{}",
        status="pending",
        attempts=MAX_ATTEMPTS - 1,
        next_attempt_at=datetime.now(UTC).isoformat(),
        created_at=datetime.now(UTC).isoformat(),
        supersede_key=capped_delete_key("mallory", "m-fake"),
    )
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=[entry])
    repo.reschedule = AsyncMock()
    repo.mark_failed = AsyncMock()
    proc = OutboxProcessor(repo, AsyncMock(return_value=DeliveryOutcome.TRANSIENT))
    await proc.drain_once()
    repo.mark_failed.assert_awaited_once_with(entry.id)
    repo.reschedule.assert_not_called()


# ── Parallel drain + multi groups (migration 0145) ─────────────────────────


def _row(entry_id: str, instance_id: str) -> OutboxEntry:
    return OutboxEntry(
        id=entry_id,
        instance_id=instance_id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="",
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(UTC).isoformat(),
        created_at=datetime.now(UTC).isoformat(),
        payload_id="p-1",
        space_id="sp-1",
        msg_id="m-1",
    )


def _repo_for(entries: list[OutboxEntry]) -> MagicMock:
    repo = MagicMock()
    repo.list_due = AsyncMock(return_value=entries)
    repo.mark_delivered = AsyncMock()
    repo.mark_failed = AsyncMock()
    repo.reschedule = AsyncMock()
    return repo


async def _until(cond, *, timeout: float = 2.0) -> None:
    """Yield to the loop until *cond()* holds (no wall-clock sleeps)."""
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0)


class _Multi:
    """A recording :class:`OutboxMulti`."""

    def __init__(self, groups, outcome=DeliveryOutcome.SUCCESS) -> None:
        self._groups = groups
        self.outcome = outcome
        self.delivered: list[list[str]] = []
        self.gate: asyncio.Event | None = None

    async def outbox_multi_groups(self, entries):
        return self._groups

    async def deliver_outbox_multi(self, group):
        self.delivered.append([e.id for e in group])
        if self.gate is not None:
            await self.gate.wait()
        return self.outcome


async def test_drain_runs_recipients_in_parallel_up_to_the_cap():
    entries = [_row(f"e{i}", f"peer-{i}") for i in range(PARALLEL_RECIPIENTS + 2)]
    repo = _repo_for(entries)
    gate = asyncio.Event()
    in_flight: set[str] = set()
    peak = 0

    async def _deliver(e):
        nonlocal peak
        in_flight.add(e.id)
        peak = max(peak, len(in_flight))
        await gate.wait()
        in_flight.discard(e.id)
        return DeliveryOutcome.SUCCESS

    drain = asyncio.create_task(OutboxProcessor(repo, _deliver).drain_once())
    await _until(lambda: len(in_flight) == PARALLEL_RECIPIENTS)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(in_flight) == PARALLEL_RECIPIENTS  # the cap holds
    gate.set()
    assert await drain == len(entries)
    assert peak == PARALLEL_RECIPIENTS
    assert repo.mark_delivered.await_count == len(entries)


async def test_one_recipient_has_one_row_in_flight_in_order():
    entries = [_row("a1", "A"), _row("a2", "A"), _row("b1", "B"), _row("a3", "A")]
    repo = _repo_for(entries)
    order: list[str] = []
    gates = {e.id: asyncio.Event() for e in entries}
    started: set[str] = set()

    async def _deliver(e):
        started.add(e.id)
        await gates[e.id].wait()
        order.append(e.id)
        return DeliveryOutcome.SUCCESS

    drain = asyncio.create_task(OutboxProcessor(repo, _deliver).drain_once())
    await _until(lambda: started == {"a1", "b1"})  # B does not wait for A
    gates["b1"].set()
    gates["a1"].set()
    await _until(lambda: "a2" in started)
    assert "a3" not in started
    gates["a2"].set()
    gates["a3"].set()
    await drain
    assert [i for i in order if i.startswith("a")] == ["a1", "a2", "a3"]


async def test_a_send_past_the_idle_timeout_is_transient(monkeypatch):
    monkeypatch.setattr(outbox_processor_mod, "SEND_IDLE_TIMEOUT_S", 0.01)
    repo = _repo_for([_row("e1", "A")])

    async def _hang(e):
        await asyncio.Event().wait()

    assert await OutboxProcessor(repo, _hang, rng=lambda: 0.5).drain_once() == 1
    repo.reschedule.assert_awaited_once()
    assert repo.reschedule.await_args.kwargs["attempts"] == 1
    repo.mark_delivered.assert_not_awaited()


async def test_a_group_outcome_applies_to_every_row():
    a, b, c = _row("a", "A"), _row("b", "B"), _row("c", "C")
    repo = _repo_for([a, b, c])
    multi = _Multi([[a, b]])
    single = AsyncMock(return_value=DeliveryOutcome.SUCCESS)
    proc = OutboxProcessor(repo, single, multi=multi)
    assert await proc.drain_once() == 3
    assert multi.delivered == [["a", "b"]]
    assert [call.args[0].id for call in single.await_args_list] == ["c"]
    assert sorted(c.args[0] for c in repo.mark_delivered.await_args_list) == [
        "a",
        "b",
        "c",
    ]


async def test_a_transient_group_reschedules_every_row():
    a, b = _row("a", "A"), _row("b", "B")
    repo = _repo_for([a, b])
    multi = _Multi([[a, b]], outcome=RetryAfter(seconds=30))
    proc = OutboxProcessor(repo, AsyncMock(), multi=multi, rng=lambda: 0.0)
    await proc.drain_once()
    assert sorted(c.args[0] for c in repo.reschedule.await_args_list) == ["a", "b"]
    repo.mark_delivered.assert_not_awaited()


async def test_a_group_member_s_other_rows_wait_for_the_group():
    a1, b1, a2, c1 = _row("a1", "A"), _row("b1", "B"), _row("a2", "A"), _row("c1", "C")
    repo = _repo_for([a1, b1, a2, c1])
    multi = _Multi([[a1, b1]])
    multi.gate = asyncio.Event()
    seen: list[str] = []

    async def _deliver(e):
        seen.append(e.id)
        return DeliveryOutcome.SUCCESS

    drain = asyncio.create_task(
        OutboxProcessor(repo, _deliver, multi=multi).drain_once()
    )
    await _until(lambda: multi.delivered and "c1" in seen)
    for _ in range(20):
        await asyncio.sleep(0)
    assert "a2" not in seen  # A is busy in the group
    multi.gate.set()
    await drain
    assert seen == ["c1", "a2"]


async def test_a_group_that_could_not_go_up_goes_per_recipient():
    a, b = _row("a", "A"), _row("b", "B")
    repo = _repo_for([a, b])
    multi = _Multi([[a, b]], outcome=None)
    single = AsyncMock(return_value=DeliveryOutcome.SUCCESS)
    await OutboxProcessor(repo, single, multi=multi).drain_once()
    assert sorted(c.args[0].id for c in single.await_args_list) == ["a", "b"]
    assert repo.mark_delivered.await_count == 2


async def test_groups_that_would_reorder_a_recipient_are_dissolved():
    # A: x before y; B: y before x — both groups cannot keep both orders.
    ax, ay, by, bx = _row("ax", "A"), _row("ay", "A"), _row("by", "B"), _row("bx", "B")
    repo = _repo_for([ax, ay, by, bx])
    multi = _Multi([[ax, bx], [ay, by]])
    seen: list[str] = []

    async def _deliver(e):
        seen.append(e.id)
        return DeliveryOutcome.SUCCESS

    assert await OutboxProcessor(repo, _deliver, multi=multi).drain_once() == 4
    assert multi.delivered == [["ay", "by"]]
    assert seen == ["ax", "bx"]
    assert repo.mark_delivered.await_count == 4


async def test_a_failing_grouper_leaves_every_row_per_recipient():
    a, b = _row("a", "A"), _row("b", "B")
    repo = _repo_for([a, b])
    multi = MagicMock()
    multi.outbox_multi_groups = AsyncMock(side_effect=RuntimeError("boom"))
    single = AsyncMock(return_value=DeliveryOutcome.SUCCESS)
    await OutboxProcessor(repo, single, multi=multi).drain_once()
    assert single.await_count == 2


async def test_a_hung_peer_costs_one_timeout_per_pass(monkeypatch):
    monkeypatch.setattr(outbox_processor_mod, "SEND_IDLE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(outbox_processor_mod, "STALL_AFTER_S", 0.0)
    hung = [_row(f"h{i}", "HUNG") for i in range(50)]
    others = [_row(f"o{i}", "OK") for i in range(3)]
    repo = _repo_for(hung + others)
    calls: list[str] = []
    never = asyncio.Event()

    async def _deliver(e):
        calls.append(e.id)
        if e.instance_id == "HUNG":
            await never.wait()
        return DeliveryOutcome.SUCCESS

    proc = OutboxProcessor(repo, _deliver, rng=lambda: 0.5)
    assert await proc.drain_once() == 53
    assert [c for c in calls if c.startswith("h")] == ["h0"]
    # Only the timed-out row spends an attempt; the rest stay pending.
    assert [c.args[0] for c in repo.reschedule.await_args_list] == ["h0"]
    assert sorted(c.args[0] for c in repo.mark_delivered.await_args_list) == [
        "o0",
        "o1",
        "o2",
    ]


async def test_a_throttled_peer_s_later_rows_wait_for_the_next_pass():
    a1, a2, b1 = _row("a1", "A"), _row("a2", "A"), _row("b1", "B")
    repo = _repo_for([a1, a2, b1])
    calls: list[str] = []

    async def _deliver(e):
        calls.append(e.id)
        return (
            RetryAfter(seconds=30)
            if e.instance_id == "A"
            else (DeliveryOutcome.SUCCESS)
        )

    await OutboxProcessor(repo, _deliver, rng=lambda: 0.0).drain_once()
    assert sorted(calls) == ["a1", "b1"]
    assert [c.args[0] for c in repo.reschedule.await_args_list] == ["a1"]


async def test_a_slow_transient_group_stalls_each_of_its_recipients(monkeypatch):
    monkeypatch.setattr(outbox_processor_mod, "STALL_AFTER_S", 0.0)
    a, b, a2, b2, c2 = (
        _row("a", "A"),
        _row("b", "B"),
        _row("a2", "A"),
        _row("b2", "B"),
        _row("c2", "C"),
    )
    repo = _repo_for([a, b, a2, b2, c2])
    multi = _Multi([[a, b]], outcome=DeliveryOutcome.TRANSIENT)
    seen: list[str] = []

    async def _deliver(e):
        seen.append(e.id)
        return DeliveryOutcome.SUCCESS

    await OutboxProcessor(repo, _deliver, multi=multi, rng=lambda: 0.0).drain_once()
    assert multi.delivered == [["a", "b"]]
    assert seen == ["c2"]
    assert sorted(c.args[0] for c in repo.reschedule.await_args_list) == ["a", "b"]


async def test_a_group_drops_a_recipient_stalled_earlier_in_the_pass(monkeypatch):
    monkeypatch.setattr(outbox_processor_mod, "STALL_AFTER_S", 0.0)
    a1, a2, b2, c2 = _row("a1", "A"), _row("a2", "A"), _row("b2", "B"), _row("c2", "C")
    repo = _repo_for([a1, a2, b2, c2])
    multi = _Multi([[a2, b2, c2]])
    seen: list[str] = []

    async def _deliver(e):
        seen.append(e.id)
        return DeliveryOutcome.TRANSIENT if e.id == "a1" else DeliveryOutcome.SUCCESS

    await OutboxProcessor(repo, _deliver, multi=multi, rng=lambda: 0.0).drain_once()
    assert seen == ["a1"]
    assert multi.delivered == [["b2", "c2"]]
    assert sorted(c.args[0] for c in repo.mark_delivered.await_args_list) == [
        "b2",
        "c2",
    ]


async def test_a_fast_failure_does_not_stall_the_recipient():
    """A refused connection costs nothing: the recipient's next rows are
    still tried, and each row counts its own attempt (a NEVER_DROP row that
    always fails must not starve the rows behind it)."""
    a1, a2, a3 = _row("a1", "A"), _row("a2", "A"), _row("a3", "A")
    repo = _repo_for([a1, a2, a3])
    calls: list[str] = []

    async def _deliver(e):
        calls.append(e.id)
        return DeliveryOutcome.TRANSIENT

    await OutboxProcessor(repo, _deliver, rng=lambda: 0.0).drain_once()
    assert calls == ["a1", "a2", "a3"]
    assert [c.args[0] for c in repo.reschedule.await_args_list] == ["a1", "a2", "a3"]


# ── GatedOutboxMulti ──────────────────────────────────────────────────────


class _Fed:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def outbox_multi_groups(self, entries):
        self.asked.append([e.id for e in entries])
        return [entries]

    async def deliver_outbox_multi(self, group):
        return DeliveryOutcome.SUCCESS


async def test_gated_multi_keeps_rows_moderation_refuses_out_of_groups():
    checked: list[str] = []

    async def _wanted(entry):
        checked.append(entry.id)
        return entry.id != "refused"

    rows = [_row(i, f"peer-{i}") for i in ("a", "refused", "b")]
    gate = GatedOutboxMulti(_Fed(), lambda: _wanted)
    assert [[e.id for e in g] for g in await gate.outbox_multi_groups(rows)] == [
        ["a", "b"]
    ]
    assert checked == ["a", "refused", "b"]
    assert await gate.deliver_outbox_multi(rows) is DeliveryOutcome.SUCCESS
    ungated = GatedOutboxMulti(_Fed(), lambda: None)
    assert len((await ungated.outbox_multi_groups(rows))[0]) == 3


async def test_gated_multi_checks_only_rows_that_could_be_grouped():
    checked: list[str] = []

    async def _wanted(entry):
        checked.append(entry.id)
        return True

    grouped = _row("grouped", "A")
    single_rows = [
        dataclasses.replace(_row("no-multi", "B"), msg_id=None),
        dataclasses.replace(_row("no-space", "C"), space_id=None),
        dataclasses.replace(
            _row("not-multi", "D"), event_type=FederationEventType.PAIRING_INTRO
        ),
    ]
    fed = _Fed()
    gate = GatedOutboxMulti(fed, lambda: _wanted)
    await gate.outbox_multi_groups([grouped, *single_rows])
    assert checked == ["grouped"]
    assert fed.asked == [["grouped"]]

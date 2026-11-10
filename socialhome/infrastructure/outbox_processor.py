"""OutboxProcessor — background retry loop for federation delivery (§4.4.2).

Reads pending rows from :class:`AbstractOutboxRepo`, hands each to a
user-supplied ``deliver(entry)`` coroutine, and on success marks the row
delivered. On failure it reschedules the row with jittered exponential
backoff; when the row hits the final attempt it is marked failed.

The retry schedule (§4.4.2) is designed to survive multi-hour outages
without hammering the peer. Base delays in seconds:

    5, 10, 20, 40, 80, 160, 320, 640, 1280, 2560, 5120, 10240, 14400

Jitter of ±30 % is applied at runtime. The 14400 cap (4 h) kicks in after
the 2.8 h step so an instance offline for a whole weekend sees only a
handful of attempts per hour instead of thousands.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ..domain.federation import MULTI_EVENT_TYPES
from ..domain.federation_retention import NEVER_DROP, TERMINAL_GRACE, is_long_lived
from ..domain.gfs_mailbox import MULTI_MAX_RECIPIENTS
from ..domain.outbox import DeliveryOutcome, RetryAfter
from ..repositories.outbox_repo import (
    PURGE_BATCH,
    AbstractOutboxRepo,
    OutboxEntry,
)

log = logging.getLogger(__name__)


#: Base retry schedule in seconds — index == attempt number after the
#: first failure. The last entry is the ceiling.
BACKOFF_SECONDS: tuple[int, ...] = (
    5,
    10,
    20,
    40,
    80,
    160,
    320,
    640,
    1280,
    2560,
    5120,
    10240,
    14400,
)

#: Attempts a 404 ``No instance found`` is treated as *transient* for.
#:
#: 404 on the inbox path means the peer could not resolve the ``inbox_id``
#: we POSTed to: either it holds no row for it, or the row it holds is
#: still provisional (empty ``remote_identity_pk``, written by
#: ``AutoPairCoordinator.request_via`` before the relay ack lands). The
#: second case is a genuine race and clears in seconds, which is why 404
#: is retried at all.
#:
#: But it was retried for the *full* ladder, so a peer that will never
#: recognise us cost 13 attempts spanning ~8 hours per envelope — and each
#: attempt rebuilds a PeerConnection, so a real deployment showed ~4,400
#: STUN bindings churning through a stuck 54-envelope backlog. Four
#: attempts covers 5+10+20+40 s of elapsed retry, which is generous for a
#: handshake race, after which the envelope is dropped with a message that
#: says what an operator should check.
PAIR_WINDOW_404_ATTEMPTS: int = 4

#: Most per-recipient deliveries in flight at once, across recipients and
#: passes. One recipient never has more than one in flight (its rows go in
#: order).
PARALLEL_RECIPIENTS: int = 8

#: Most multi uploads in flight at once (v_91). They go to a GFS, not to a
#: peer, so they never wait for a :data:`PARALLEL_RECIPIENTS` slot — a hung
#: peer cannot hold back a queued broadcast.
MULTI_PARALLEL: int = 4

#: Due rows one drain pass reads (rows of busy recipients left out). As
#: large as one multi-recipient group (``MULTI_MAX_RECIPIENTS``), so a
#: broadcast to 500 mailbox seats goes up as ONE upload in one pass, not in
#: 50-row chunks over ten passes. Rows are small (a ref row is ~230 B).
DRAIN_BATCH: int = MULTI_MAX_RECIPIENTS

#: Longest one delivery (one row, or one multi upload) may take before it
#: counts as :attr:`DeliveryOutcome.TRANSIENT` — a hung peer must not hold a
#: slot (and its recipients) for the rest of the pass.
SEND_IDLE_TIMEOUT_S: float = 120.0

#: A failed delivery that took at least this long (or timed out, or was
#: throttled) stalls its recipient for the rest of its pass's rows. A fast failure
#: (a refused connection) costs nothing, so the recipient's next rows are
#: still tried and every row keeps counting its attempts.
STALL_AFTER_S: float = 10.0

#: How long :meth:`OutboxProcessor.stop` waits for the loop and the
#: deliveries in flight before cancelling them (their rows stay pending).
STOP_DEADLINE_S: float = 5.0

#: How far to perturb the base delay — ±30%.
JITTER_RATIO: float = 0.30

#: Max attempts before an entry is moved to ``failed``. Matches the length
#: of :data:`BACKOFF_SECONDS`.
MAX_ATTEMPTS: int = len(BACKOFF_SECONDS)

# ``NEVER_DROP`` is imported from :mod:`socialhome.domain.federation_retention`
# (the single source of truth) and re-exported under this name so the
# retry-pinning logic below and ``infrastructure.__init__``'s re-export still
# resolve. It is used by ``drain_once`` to pin structural / security events on
# the ceiling backoff instead of marking them failed.


#: Ceiling on how long a :class:`RetryAfter` may push one retry out — the
#: backoff ceiling. A peer's ``Retry-After`` is a hint about its own window;
#: honouring an arbitrary value would let it park an entry indefinitely.
MAX_RETRY_AFTER_S: float = float(BACKOFF_SECONDS[-1])


#: Delivery callback signature. Return one of the :class:`DeliveryOutcome`
#: values or a :class:`RetryAfter`; raising is treated the same as
#: :attr:`DeliveryOutcome.TRANSIENT`.
Deliver = Callable[[OutboxEntry], Awaitable["DeliveryOutcome | RetryAfter"]]


class OutboxMulti(Protocol):
    """Sends due rows of one multi payload as ONE multi-recipient upload
    (v_91; implemented by ``FederationService`` — the only multi sender).

    ``outbox_multi_groups`` returns groups (≥ 2 rows each, every row's
    recipient at most once) that may go up together; rows in no group go
    one by one. ``deliver_outbox_multi`` returns the outcome that applies to
    every row of *group*, or ``None`` when the group could not go up as one
    (each row is then delivered on its own).

    Every envelope built from a multi payload carries its stored
    ``msg_id``, so an upload the GFS accepted but whose answer was lost is
    retried under the same id and the receivers' replay cache drops the
    second copy.
    """

    async def outbox_multi_groups(
        self, entries: list[OutboxEntry]
    ) -> list[list[OutboxEntry]]: ...

    async def deliver_outbox_multi(
        self, group: list[OutboxEntry]
    ) -> DeliveryOutcome | RetryAfter | None: ...


#: The moderation queue's per-row check (``still_wanted``), or a resolver of
#: it that answers ``None`` until the moderation service is wired.
StillWanted = Callable[[OutboxEntry], Awaitable[bool]]


class GatedOutboxMulti:
    """An :class:`OutboxMulti` that keeps rows the moderation check refuses
    out of every group: those stay single, and the delivery callback drops
    them (PERMANENT). The check (``still_wanted_of()``, resolved per pass)
    runs only on rows that could be grouped — a ref row of a multi payload
    with a space, of a :data:`MULTI_EVENT_TYPES` type — so a single row is
    not checked twice."""

    __slots__ = ("_inner", "_still_wanted_of")

    def __init__(
        self,
        inner: OutboxMulti,
        still_wanted_of: Callable[[], StillWanted | None],
    ) -> None:
        self._inner = inner
        self._still_wanted_of = still_wanted_of

    async def outbox_multi_groups(
        self, entries: list[OutboxEntry]
    ) -> list[list[OutboxEntry]]:
        still_wanted = self._still_wanted_of()
        if still_wanted is not None:
            entries = [e for e in entries if _groupable(e) and await still_wanted(e)]
        return await self._inner.outbox_multi_groups(entries)

    async def deliver_outbox_multi(
        self, group: list[OutboxEntry]
    ) -> DeliveryOutcome | RetryAfter | None:
        return await self._inner.deliver_outbox_multi(group)


def _groupable(entry: OutboxEntry) -> bool:
    """Whether *entry* could ride a multi upload at all."""
    return (
        entry.msg_id is not None
        and entry.space_id is not None
        and entry.event_type in MULTI_EVENT_TYPES
    )


#: Optional follow-up to the retention sweep. Runs after expired rows have
#: been flipped to ``failed``, so it sees the outbox as the sweep left it
#: (the unpair-tombstone purge keys off "no UNPAIR still pending").
AfterPrune = Callable[[], Awaitable[object]]


class OutboxProcessor:
    """Long-running coroutine that drains the outbox on a timer.

    Follows the same ``_stop: asyncio.Event`` lifecycle as every other
    scheduler in :mod:`socialhome.infrastructure` (see
    :class:`ReplayCachePruneScheduler`). ``start()`` kicks off the
    loop, ``stop()`` sets the event and awaits the loop and the deliveries
    in flight up to a 5-second deadline before cancelling.

    A pass never waits for its deliveries (:meth:`_start_pass`): it starts
    them and returns, so a hung peer cannot delay the next pass — or a
    multi upload a broadcast just queued. Recipients with a delivery in
    flight are *busy* (in memory only): their rows are left out of every
    pass until it ends, so each recipient's rows still go one at a time in
    due order. Nothing is persisted, so a crash leaves those rows pending.
    """

    __slots__ = (
        "_after_prune",
        "_busy",
        "_deliver",
        "_inflight",
        "_jitter",
        "_last_prune",
        "_multi",
        "_multi_slots",
        "_peer_slots",
        "_poll_interval",
        "_prune_interval",
        "_repo",
        "_stop",
        "_task",
        "_wake",
    )

    def __init__(
        self,
        repo: AbstractOutboxRepo,
        deliver: Deliver,
        *,
        poll_interval_seconds: float = 5.0,
        prune_interval_seconds: float = 3600.0,
        rng: Callable[[], float] | None = None,
        after_prune: AfterPrune | None = None,
        multi: OutboxMulti | None = None,
    ) -> None:
        self._repo = repo
        self._multi = multi
        self._after_prune = after_prune
        self._deliver = deliver
        self._poll_interval = poll_interval_seconds
        self._prune_interval = prune_interval_seconds
        # ``_last_prune`` is a ``time.monotonic()`` stamp, or ``None`` for
        # "never pruned yet" so the FIRST loop tick always prunes regardless
        # of the machine's uptime. (A plain ``0.0`` sentinel would NOT prune
        # on the first tick on a freshly-booted host, where ``time.monotonic()``
        # can be < ``prune_interval`` — the gate ``now - 0.0 >= interval``
        # would be false.) Pruning at startup is harmless — it only marks
        # already-expired rows failed — and keeps the cold-start outbox clean.
        # Tests set this explicitly to gate cadence.
        self._last_prune: float | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        #: Set by :meth:`wake` (or :meth:`stop`): the loop's wait ends early.
        self._wake = asyncio.Event()
        # ``rng`` is injectable for deterministic tests. Default uses
        # ``random.random`` — uniform in [0, 1).
        self._jitter = rng or random.random
        #: Recipients whose rows a running pass owns (in flight or next in
        #: its line) — left out of every other pass until they are done.
        self._busy: set[str] = set()
        #: Delivery tasks still running (:meth:`stop` and
        #: :meth:`drain_once` wait for them).
        self._inflight: set[asyncio.Task] = set()
        self._peer_slots = asyncio.Semaphore(PARALLEL_RECIPIENTS)
        self._multi_slots = asyncio.Semaphore(MULTI_PARALLEL)

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="OutboxProcessor")

    def wake(self) -> None:
        """Run a drain pass now instead of at the next poll — a broadcast
        just queued a multi payload (v_91). A wake during a pass runs one
        more pass after it."""
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + STOP_DEADLINE_S
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=STOP_DEADLINE_S)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None
        if self._inflight:
            # Deliveries in flight finish (or are cancelled — their rows
            # stay pending) within the same deadline.
            _done, late = await asyncio.wait(
                set(self._inflight), timeout=max(deadline - time.monotonic(), 0.0)
            )
            for task in late:
                task.cancel()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self._start_pass()
            except Exception:
                log.exception("OutboxProcessor tick failed")
            # §4.4.7 retention sweep — runs on its own (slow) cadence folded
            # into the drain loop. Best-effort: a failure here must never
            # crash the drain loop, hence the separate try/except.
            now = time.monotonic()
            if (
                self._last_prune is None
                or now - self._last_prune >= self._prune_interval
            ):
                self._last_prune = now
                try:
                    expired = await self.prune_once()
                    if expired:
                        log.info(
                            "OutboxProcessor: expired %d entries past retention",
                            expired,
                        )
                except Exception:
                    log.exception("OutboxProcessor prune tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    # ── Single tick ────────────────────────────────────────────────────

    async def drain_once(self, *, limit: int = DRAIN_BATCH) -> int:
        """One pass (:meth:`_start_pass`), then wait until every delivery in
        flight has ended. Returns the count of due rows the pass took."""
        count = await self._start_pass(limit=limit)
        # A task starts the next unit of its line before it ends, so the
        # set is complete once every task in it is done.
        while pending := [t for t in self._inflight if not t.done()]:
            await asyncio.wait(pending)
        return count

    async def _start_pass(self, *, limit: int = DRAIN_BATCH) -> int:
        """Start delivering up to ``limit`` due rows of recipients that are
        not busy; return without waiting for them. Returns the count taken.

        Each of the pass's recipients is busy until its last row of the pass
        is done; its rows go one at a time, in due order. Rows of one stored
        payload the :class:`OutboxMulti` groups go up as ONE upload whose
        outcome applies to each (a group is formed from recipients that are
        not busy only — a busy one keeps its row for a later pass, under the
        same stable ``msg_id``). Per-recipient deliveries share
        :data:`PARALLEL_RECIPIENTS` slots; multi uploads use their own
        :data:`MULTI_PARALLEL` slots and never wait for a per-recipient one.
        Every delivery runs under :data:`SEND_IDLE_TIMEOUT_S`.

        Once a recipient's delivery times out, is throttled
        (:class:`RetryAfter`) or fails after :data:`STALL_AFTER_S` or more,
        its remaining rows of the pass are not attempted: they stay
        ``pending`` for a later pass, with no attempt counted — one dead
        peer costs one timeout per pass, not one per queued row. A fast
        failure does not stall. A group that fails so stalls each of its
        recipients; a later group drops a stalled recipient (and goes per
        row when fewer than two are left).

        Errors raised by the deliver callback (and timeouts) are treated as
        :attr:`DeliveryOutcome.TRANSIENT`. Rows whose ``attempts`` would
        exceed :data:`MAX_ATTEMPTS` are marked failed rather than
        rescheduled. PERMANENT outcomes (4xx other than 429) are dropped
        immediately; a :class:`RetryAfter` is TRANSIENT with a delay floor.
        """
        busy = frozenset(self._busy)
        entries = [
            e
            for e in await self._repo.list_due(limit, exclude=busy)
            if e.instance_id not in self._busy
        ]
        if not entries:
            return 0
        units = _order_units(entries, await self._groups(entries))
        #: Per recipient, the indexes of the units it is in, in run order.
        queues: dict[str, deque[int]] = {}
        for idx, unit in enumerate(units):
            for entry in unit:
                queues.setdefault(entry.instance_id, deque()).append(idx)
        self._busy.update(queues)
        started: set[int] = set()
        #: Recipients whose delivery this pass ended transient — skipped.
        stalled: set[str] = set()

        def _ready(idx: int) -> bool:
            return idx not in started and all(
                queues[e.instance_id][0] == idx for e in units[idx]
            )

        def _start(idx: int) -> None:
            started.add(idx)
            task = asyncio.create_task(_run(idx))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

        async def _run(idx: int) -> None:
            unit = units[idx]
            try:
                if any(e.instance_id not in stalled for e in unit):
                    slots = self._multi_slots if len(unit) > 1 else self._peer_slots
                    async with slots:
                        # Re-read once a slot is ours: a recipient may have
                        # stalled while this unit waited for it.
                        live = [e for e in unit if e.instance_id not in stalled]
                        if live:
                            await self._run_unit(live, stalled)
            finally:
                heads: list[int] = []
                for entry in unit:
                    queue = queues[entry.instance_id]
                    queue.popleft()
                    if queue:
                        heads.append(queue[0])
                    else:
                        self._busy.discard(entry.instance_id)
                if self._stop.is_set():
                    # Stopping: the rest of the line stays pending.
                    heads = []
                for head in heads:
                    if _ready(head):
                        _start(head)

        for idx in range(len(units)):
            if _ready(idx):
                _start(idx)
        return len(entries)

    async def _groups(self, entries: list[OutboxEntry]) -> list[list[OutboxEntry]]:
        if self._multi is None:
            return []
        try:
            return await self._multi.outbox_multi_groups(entries)
        except Exception:
            log.warning("OutboxProcessor: grouping failed", exc_info=True)
            return []

    async def _run_unit(self, unit: list[OutboxEntry], stalled: set[str]) -> None:
        """Deliver one row, or one multi group; never raises. A recipient
        whose delivery is slow to fail or throttled joins *stalled*."""
        try:
            if len(unit) > 1 and self._multi is not None:
                started = time.monotonic()
                group = await self._attempt(
                    unit[0].id, self._multi.deliver_outbox_multi(unit)
                )
                if group is not None:
                    if _stalls(group, time.monotonic() - started):
                        stalled.update(e.instance_id for e in unit)
                    for entry in unit:
                        await self._settle(entry, group)
                    return
            for entry in unit:
                started = time.monotonic()
                outcome = await self._attempt(entry.id, self._deliver(entry))
                outcome = outcome or DeliveryOutcome.TRANSIENT
                if _stalls(outcome, time.monotonic() - started):
                    stalled.add(entry.instance_id)
                await self._settle(entry, outcome)
        except Exception:
            log.exception("OutboxProcessor: delivery bookkeeping failed")

    async def _attempt(
        self,
        label: str,
        delivery: Awaitable[DeliveryOutcome | RetryAfter | None],
    ) -> DeliveryOutcome | RetryAfter | None:
        try:
            return await asyncio.wait_for(delivery, timeout=SEND_IDLE_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "OutboxProcessor delivery of %s timed out after %ss",
                label,
                SEND_IDLE_TIMEOUT_S,
            )
        except Exception as exc:
            log.warning(
                "OutboxProcessor delivery raised for %s: %s",
                label,
                exc,
                exc_info=True,
            )
        return DeliveryOutcome.TRANSIENT

    async def _settle(
        self, entry: OutboxEntry, outcome: DeliveryOutcome | RetryAfter
    ) -> None:
        """Apply one delivery outcome to *entry*'s row."""
        if outcome is DeliveryOutcome.SUCCESS:
            await self._repo.mark_delivered(entry.id)
            return
        if outcome is DeliveryOutcome.PERMANENT:
            # 4xx — peer will never accept this envelope (replay cache
            # hit, expired timestamp, banned sender, malformed body).
            # Drop the row even for :data:`NEVER_DROP` events: a
            # 410 ``Replay detected`` means the receiver already has
            # the event, so the security invariant is satisfied.
            log.warning(
                "OutboxProcessor: peer permanently rejected %s — dropping",
                entry.id,
            )
            await self._repo.mark_failed(entry.id)
            return

        # TRANSIENT, or a RetryAfter (a 429 that named its window).
        floor_s = (
            self._retry_after_floor(outcome.seconds)
            if isinstance(outcome, RetryAfter)
            else 0.0
        )
        new_attempts = entry.attempts + 1
        if new_attempts >= MAX_ATTEMPTS:
            # §4.4.7: structural / security events keep retrying on
            # the ceiling backoff — losing a ban or key revocation
            # silently would create an attacker-friendly window. A
            # LONG_LIVED event (v_66: a moment's delete) does the same
            # until its 30-day ``expires_at``, when the retention sweep
            # marks it failed — unless it was relayed past its origin's
            # cap (F2), which gives up like any ordinary event.
            if entry.event_type in NEVER_DROP or is_long_lived(
                entry.event_type, entry.supersede_key
            ):
                log.info(
                    "OutboxProcessor: %s entry %s past MAX_ATTEMPTS"
                    " — pinning at ceiling backoff",
                    entry.event_type,
                    entry.id,
                )
                delay = max(self._delay_for(MAX_ATTEMPTS), floor_s)
                next_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
                await self._repo.reschedule(
                    entry.id,
                    next_at,
                    attempts=new_attempts,
                )
                return
            log.warning(
                "OutboxProcessor giving up on %s after %d attempts",
                entry.id,
                new_attempts,
            )
            await self._repo.mark_failed(entry.id)
            return
        delay = max(self._delay_for(new_attempts), floor_s)
        next_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        await self._repo.reschedule(
            entry.id,
            next_at,
            attempts=new_attempts,
        )

    async def prune_once(self) -> int:
        """Retention sweep (§4.4.7). First, drop rows addressed to a
        household that is gone (no ``remote_instances`` row); then:
        1. flip pending rows past their expires_at to ``failed`` (NEVER_DROP
           rows have expires_at=NULL and are skipped);
        2. DELETE terminal (delivered/failed) rows older than TERMINAL_GRACE,
           in bounded batches, so the queue never accumulates tombstones
           (and a pre-change historical backlog is reclaimed over time).
        Then the optional ``after_prune`` hook (best-effort).
        Returns orphaned + expired + purged count.
        """
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        # Rows for a household whose ``remote_instances`` row is gone (a
        # crash between a teardown's row delete and its outbox purge) can
        # never be delivered: drop them outright.
        orphaned = await self._repo.purge_orphaned()
        if orphaned:
            log.info(
                "OutboxProcessor: dropped %d rows for households that are gone",
                orphaned,
            )
        expired = await self._repo.expire_past_retention(now_iso)
        cutoff_iso = (now - TERMINAL_GRACE).isoformat()
        purged = 0
        for _ in range(20):  # bounded: up to 20 * batch rows per tick
            n = await self._repo.purge_terminal(cutoff_iso)
            purged += n
            # ``PURGE_BATCH`` is the repo's default ``limit`` — comparing to
            # the same constant keeps the "drained" sentinel from drifting.
            if n < PURGE_BATCH:  # fewer than a full batch ⇒ drained
                break
        if purged:
            log.info("OutboxProcessor: purged %d terminal outbox rows", purged)
        if self._after_prune is not None:
            try:
                await self._after_prune()
            except Exception:
                # Best-effort like the sweep itself — never costs the prune.
                log.exception("OutboxProcessor after-prune hook failed")
        return orphaned + expired + purged

    # ── Backoff math (pure) ────────────────────────────────────────────

    def _retry_after_floor(self, seconds: float) -> float:
        """The minimum next delay for a peer's ``Retry-After``.

        Capped at :data:`MAX_RETRY_AFTER_S`, then jittered UP by up to
        :data:`JITTER_RATIO` — never below what the peer asked, but spread so
        every envelope it throttled in one burst does not come back on the
        same tick and re-trip the same window.
        """
        base = min(max(seconds, 0.0), MAX_RETRY_AFTER_S)
        return base * (1.0 + self._jitter() * JITTER_RATIO)

    def _delay_for(self, attempt: int) -> float:
        """Return a jittered delay in seconds for the given attempt count.

        ``attempt`` is 1-based (first retry is ``1``). Attempts beyond
        ``len(BACKOFF_SECONDS)`` reuse the last (ceiling) base delay.
        """
        idx = min(attempt, len(BACKOFF_SECONDS)) - 1
        base = BACKOFF_SECONDS[idx]
        # Convert jitter sample ``[0,1)`` into the range ``[-1, 1]`` then
        # scale by JITTER_RATIO so the perturbation is ±30 %.
        sample = self._jitter()
        signed = (sample * 2.0) - 1.0
        return max(1.0, base * (1.0 + signed * JITTER_RATIO))


def _stalls(outcome: DeliveryOutcome | RetryAfter, elapsed_s: float) -> bool:
    """Whether a delivery leaves its recipient stalled for the pass: it was
    throttled (:class:`RetryAfter`), or failed TRANSIENT after
    :data:`STALL_AFTER_S` or more (a timeout always does)."""
    if isinstance(outcome, RetryAfter):
        return True
    return outcome is DeliveryOutcome.TRANSIENT and elapsed_s >= STALL_AFTER_S


def _order_units(
    entries: list[OutboxEntry], groups: list[list[OutboxEntry]]
) -> list[list[OutboxEntry]]:
    """The pass's delivery units — each multi group, and every other row on
    its own — in run order (by their first row's place in *entries*).

    A unit starts at its first row's place, so a group can run one
    recipient's row ahead of that recipient's earlier rows when two groups
    cross. Such a group is dissolved into single rows (a single row never
    can be the one out of order), so every recipient's rows still go in
    their due order and the run order can never deadlock.
    """
    position = {entry.id: i for i, entry in enumerate(entries)}
    live = [
        g
        for g in ([e for e in group if e.id in position] for group in groups)
        if len(g) >= 2 and len({e.instance_id for e in g}) == len(g)
    ]
    while True:
        grouped = {e.id: g_idx for g_idx, g in enumerate(live) for e in g}
        start = [min(position[e.id] for e in g) for g in live]
        last: dict[str, int] = {}
        crossing: int | None = None
        for entry in entries:
            g_idx = grouped.get(entry.id)
            at = start[g_idx] if g_idx is not None else position[entry.id]
            if g_idx is not None and at < last.get(entry.instance_id, -1):
                crossing = g_idx
                break
            last[entry.instance_id] = at
        if crossing is None:
            break
        del live[crossing]
    units = [(min(position[e.id] for e in g), g) for g in live]
    units += [(position[e.id], [e]) for e in entries if e.id not in grouped]
    units.sort(key=lambda u: u[0])
    return [unit for _at, unit in units]

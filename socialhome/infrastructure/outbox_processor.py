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
import enum
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from ..domain.federation_retention import NEVER_DROP, TERMINAL_GRACE
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


class DeliveryOutcome(enum.Enum):
    """Result of a single outbound delivery attempt.

    The processor uses the outcome to decide between three terminal
    paths:

    * :attr:`SUCCESS` — receiver returned 2xx. Row is marked delivered.
    * :attr:`PERMANENT` — receiver returned 4xx (already seen via
      replay cache, timestamp too old, banned, malformed envelope).
      Retrying will never succeed; row is marked failed immediately
      regardless of attempt count or :data:`NEVER_DROP` membership.
    * :attr:`TRANSIENT` — 5xx, timeout, DNS failure, connection reset.
      Row is rescheduled with jittered backoff, respecting
      :data:`NEVER_DROP` past :data:`MAX_ATTEMPTS`.
    """

    SUCCESS = "success"
    PERMANENT = "permanent"
    TRANSIENT = "transient"


#: Delivery callback signature. Return one of the :class:`DeliveryOutcome`
#: values; raising is treated the same as :attr:`DeliveryOutcome.TRANSIENT`.
Deliver = Callable[[OutboxEntry], Awaitable[DeliveryOutcome]]


class OutboxProcessor:
    """Long-running coroutine that drains the outbox on a timer.

    Follows the same ``_stop: asyncio.Event`` lifecycle as every other
    scheduler in :mod:`socialhome.infrastructure` (see
    :class:`ReplayCachePruneScheduler`). ``start()`` kicks off the
    loop, ``stop()`` sets the event and awaits graceful shutdown up
    to a 5-second deadline before cancelling.
    """

    __slots__ = (
        "_repo",
        "_deliver",
        "_poll_interval",
        "_prune_interval",
        "_last_prune",
        "_task",
        "_stop",
        "_jitter",
    )

    def __init__(
        self,
        repo: AbstractOutboxRepo,
        deliver: Deliver,
        *,
        poll_interval_seconds: float = 5.0,
        prune_interval_seconds: float = 3600.0,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._repo = repo
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
        # ``rng`` is injectable for deterministic tests. Default uses
        # ``random.random`` — uniform in [0, 1).
        self._jitter = rng or random.random

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="OutboxProcessor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.drain_once()
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
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._poll_interval,
                )
            except asyncio.TimeoutError:
                continue

    # ── Single tick ────────────────────────────────────────────────────

    async def drain_once(self, *, limit: int = 50) -> int:
        """Process up to ``limit`` due entries. Returns count processed.

        Errors raised by the deliver callback are caught and treated as
        :attr:`DeliveryOutcome.TRANSIENT`. Rows whose ``attempts`` would
        exceed :data:`MAX_ATTEMPTS` are marked failed rather than
        rescheduled. 4xx outcomes are dropped immediately.
        """
        entries = await self._repo.list_due(limit)
        if not entries:
            return 0
        for entry in entries:
            try:
                outcome = await self._deliver(entry)
            except Exception as exc:
                log.warning(
                    "OutboxProcessor delivery raised for %s: %s",
                    entry.id,
                    exc,
                )
                outcome = DeliveryOutcome.TRANSIENT
            if outcome is DeliveryOutcome.SUCCESS:
                await self._repo.mark_delivered(entry.id)
                continue
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
                continue

            new_attempts = entry.attempts + 1
            if new_attempts >= MAX_ATTEMPTS:
                # §4.4.7: structural / security events keep retrying on
                # the ceiling backoff — losing a ban or key revocation
                # silently would create an attacker-friendly window.
                if entry.event_type in NEVER_DROP:
                    log.info(
                        "OutboxProcessor: %s entry %s past MAX_ATTEMPTS"
                        " — pinning at ceiling backoff",
                        entry.event_type,
                        entry.id,
                    )
                    delay = self._delay_for(MAX_ATTEMPTS)
                    next_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
                    await self._repo.reschedule(
                        entry.id,
                        next_at,
                        attempts=new_attempts,
                    )
                    continue
                log.warning(
                    "OutboxProcessor giving up on %s after %d attempts",
                    entry.id,
                    new_attempts,
                )
                await self._repo.mark_failed(entry.id)
                continue
            delay = self._delay_for(new_attempts)
            next_at = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat()
            await self._repo.reschedule(
                entry.id,
                next_at,
                attempts=new_attempts,
            )
        return len(entries)

    async def prune_once(self) -> int:
        """Retention sweep (§4.4.7). Two phases:
        1. flip pending rows past their expires_at to ``failed`` (NEVER_DROP
           rows have expires_at=NULL and are skipped);
        2. DELETE terminal (delivered/failed) rows older than TERMINAL_GRACE,
           in bounded batches, so the queue never accumulates tombstones
           (and a pre-change historical backlog is reclaimed over time).
        Returns expired + purged count.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
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
        return expired + purged

    # ── Backoff math (pure) ────────────────────────────────────────────

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

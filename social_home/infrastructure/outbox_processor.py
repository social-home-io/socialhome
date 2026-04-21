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
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from ..domain.federation import FederationEventType
from ..repositories.outbox_repo import AbstractOutboxRepo, OutboxEntry


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

#: How far to perturb the base delay — ±30%.
JITTER_RATIO: float = 0.30

#: Max attempts before an entry is moved to ``failed``. Matches the length
#: of :data:`BACKOFF_SECONDS`.
MAX_ATTEMPTS: int = len(BACKOFF_SECONDS)

#: Event types that must NEVER be marked ``failed`` regardless of attempt
#: count (§4.4.7).  These carry security or structural state — admin key
#: shares, bans, unpair signals, key revocations — that the receiver
#: must eventually see, even if the peer is offline for weeks.  Instead
#: of giving up after :data:`MAX_ATTEMPTS`, we keep retrying on the
#: ceiling backoff (4 hours) indefinitely.
NEVER_DROP: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        FederationEventType.SPACE_ADMIN_KEY_SHARE,
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.UNPAIR,
    }
)


#: Delivery callback signature. Return ``True`` on success, ``False`` on a
#: transport failure the processor should retry. Raising is treated the
#: same as returning ``False``.
Deliver = Callable[[OutboxEntry], Awaitable[bool]]


class OutboxProcessor:
    """Long-running coroutine that drains the outbox on a timer.

    Follows the same ``_stop: asyncio.Event`` lifecycle as every other
    scheduler in :mod:`social_home.infrastructure` (see
    :class:`ReplayCachePruneScheduler`). ``start()`` kicks off the
    loop, ``stop()`` sets the event and awaits graceful shutdown up
    to a 5-second deadline before cancelling.
    """

    __slots__ = (
        "_repo",
        "_deliver",
        "_poll_interval",
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
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._repo = repo
        self._deliver = deliver
        self._poll_interval = poll_interval_seconds
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
        transport failures. Rows whose ``attempts`` would exceed
        :data:`MAX_ATTEMPTS` are marked failed rather than rescheduled.
        """
        entries = await self._repo.list_due(limit)
        if not entries:
            return 0
        for entry in entries:
            try:
                ok = await self._deliver(entry)
            except Exception as exc:
                log.warning(
                    "OutboxProcessor delivery raised for %s: %s",
                    entry.id,
                    exc,
                )
                ok = False
            if ok:
                await self._repo.mark_delivered(entry.id)
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

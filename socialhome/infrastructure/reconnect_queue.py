"""ReconnectSyncQueue — priority-ordered work queue for reconnect-time sync.

When a peer comes back online, we need to drain a backlog of tasks
(deliver outbox entries, fetch space sync, replay missed config events,
etc.). Doing all of these at once thunders the herd; doing them in
arbitrary order risks a low-priority bulk import starving a high-priority
config event.

This queue applies the §4.4 priority schedule:

| Priority | Use case                                       |
|----------|------------------------------------------------|
| P1       | Security-critical (admin key share, ban, unpair) |
| P2       | Structural events (space create/dissolve, config) |
| P3       | User membership (joined, left)                  |
| P4       | DM relay                                        |
| P5       | Space content (posts, comments)                 |
| P6       | Calendar / tasks / pages                        |
| P7       | Bulk history sync                               |

A semaphore (default :data:`SYNC_CONCURRENCY`) caps concurrent workers
so we don't saturate the network or the peer.

The queue is in-memory and per-process; it survives no restarts. The
canonical retry queue is :class:`OutboxProcessor`. This queue is the
*orchestrator* that decides what order to drain.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ─── Constants ───────────────────────────────────────────────────────────

#: Maximum simultaneous workers running tasks from the queue.
SYNC_CONCURRENCY: int = 4

#: Priority floors. Higher number = lower priority. Smaller heap value
#: → drained first.
P1_SECURITY: int = 1
P2_STRUCTURAL: int = 2
P3_MEMBERSHIP: int = 3
P4_DM: int = 4
P5_CONTENT: int = 5
P6_PRODUCTIVITY: int = 6
P7_BULK: int = 7


@dataclass(slots=True, order=True)
class _QueueItem:
    """heapq element. Sorted by ``(priority, sequence)`` for FIFO within priority."""

    priority: int
    sequence: int
    coro_factory: Callable[[], Awaitable[None]] = field(compare=False)
    description: str = field(compare=False, default="")


class ReconnectSyncQueue:
    """Priority-ordered async work queue with bounded concurrency.

    The caller supplies a coroutine *factory* (a zero-arg callable
    returning an awaitable) so the work isn't started until a worker
    picks it up. This avoids holding open resources while items wait.

    Lifecycle:

        q = ReconnectSyncQueue()
        await q.start()
        q.enqueue(P5_CONTENT, my_coroutine_factory, "deliver post xyz")
        ...
        await q.stop()

    Calling :meth:`stop` waits for in-flight tasks to finish — it does
    NOT cancel them. To force-cancel, use :meth:`cancel_pending`.
    """

    __slots__ = (
        "_concurrency",
        "_heap",
        "_seq",
        "_workers",
        "_event",
        "_stop",
        "_lock",
    )

    def __init__(self, concurrency: int = SYNC_CONCURRENCY) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency must be > 0")
        self._concurrency = concurrency
        self._heap: list[_QueueItem] = []
        self._seq = itertools.count()
        self._workers: list[asyncio.Task] = []
        # ``_event`` is the work-available signal (enqueue → set).
        # ``_stop`` is the lifecycle flag per CLAUDE.md "Schedulers"
        # invariant — set by ``stop()`` to tell workers to drain + exit.
        self._event = asyncio.Event()
        self._stop = asyncio.Event()
        self._stop.set()  # initial state: not running
        self._lock = asyncio.Lock()

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spin up workers. Idempotent — a second call is a no-op."""
        if not self._stop.is_set():
            return
        self._stop.clear()
        loop = asyncio.get_running_loop()
        for i in range(self._concurrency):
            self._workers.append(
                loop.create_task(
                    self._worker(),
                    name=f"ReconnectSyncQueueWorker-{i}",
                )
            )

    async def stop(self) -> None:
        """Signal workers to exit and wait for them to drain."""
        self._stop.set()
        # Wake everyone up so pending waits on ``_event`` unblock.
        self._event.set()
        for w in self._workers:
            await self._await_safely(w)
        self._workers.clear()

    async def cancel_pending(self) -> int:
        """Drop all queued items without running them. Returns count cancelled."""
        async with self._lock:
            n = len(self._heap)
            self._heap.clear()
        return n

    @staticmethod
    async def _await_safely(task: asyncio.Task) -> None:
        try:
            await task
        except asyncio.CancelledError, Exception:
            pass

    # ─── Public API ───────────────────────────────────────────────────────

    def enqueue(
        self,
        priority: int,
        coro_factory: Callable[[], Awaitable[None]],
        description: str = "",
    ) -> None:
        """Schedule *coro_factory* at the given priority."""
        if not (P1_SECURITY <= priority <= P7_BULK):
            raise ValueError(
                f"priority must be P1..P7 ({P1_SECURITY}..{P7_BULK}), got {priority}"
            )
        item = _QueueItem(
            priority=priority,
            sequence=next(self._seq),
            coro_factory=coro_factory,
            description=description,
        )
        heapq.heappush(self._heap, item)
        self._event.set()

    def pending_count(self) -> int:
        return len(self._heap)

    # ─── Internal worker loop ────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            if self._stop.is_set() and not self._heap:
                return
            item = await self._next_item()
            if item is None:
                return
            try:
                await item.coro_factory()
            except Exception as exc:
                log.warning(
                    "ReconnectSyncQueue task failed (%s P%d): %s",
                    item.description,
                    item.priority,
                    exc,
                )

    async def _next_item(self) -> _QueueItem | None:
        while True:
            async with self._lock:
                if self._heap:
                    return heapq.heappop(self._heap)
                if self._stop.is_set():
                    return None
                self._event.clear()
            await self._event.wait()

"""Background fan-out of accepted channel items.

A publish route must never wait on N seats: an accepted item is handed to a
small pool of workers (bounded backlog, a per-key share of it) that push it
to every live seat and queue an offline copy for the seats seen within the
queue TTL (``GfsEnvelopeRelay.fan_out_relay``). Each key — ``"ch:" +
channel_id`` — is pinned to one worker by a hash of the key, so one
channel's items go out in publish order. The frame never names the
publisher.

Lifecycle follows the CLAUDE.md scheduler pattern: :meth:`start` spawns
:data:`FAN_OUT_WORKERS` workers that loop ``while not self._stop.is_set()``;
:meth:`stop` first stops accepting (``_closing``), lets the workers drain
what was accepted (bounded by :data:`FAN_OUT_STOP_DRAIN_S`), then sets
``_stop`` and waits for them — a worker always finishes the item it is on
instead of being cancelled mid-write.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .envelope_relay import GfsEnvelopeRelay

log = logging.getLogger(__name__)

#: Background fan-out workers.
FAN_OUT_WORKERS: int = 2

#: Accepted items one worker may hold, and the share of that one KEY (one
#: channel) may take. Past either the publish answers 503 (the household's
#: retry queue backs off) — so one busy channel can't refuse every other.
FAN_OUT_BACKLOG: int = 256
FAN_OUT_BACKLOG_PER_KEY: int = 64

#: How long :meth:`GfsFanOutWorkers.stop` waits for accepted items to go out
#: before giving up on the rest (seconds).
FAN_OUT_STOP_DRAIN_S: float = 5.0

#: How often an idle worker re-checks the stop event (seconds).
FAN_OUT_IDLE_POLL_S: float = 0.5

#: Resolves a job's targets in the worker: ``(targets, queue_ok)`` — the
#: households to push to, and the ones that may get an offline copy.
FanOutResolver = Callable[[], Awaitable[tuple[list[str], set[str]]]]


@dataclass(slots=True, frozen=True)
class _FanOutJob:
    """One accepted item waiting for the background fan-out."""

    key: str
    publisher_id: str
    frame: dict
    resolve: FanOutResolver
    #: The cluster audience the item is forwarded for (``(kind, key)``, see
    #: :mod:`.cluster_relay`).
    audience: tuple[str, str]


class GfsFanOutWorkers:
    """Bounded background workers that push accepted items to their seats."""

    __slots__ = ("_closing", "_pending", "_queues", "_relay", "_stop", "_tasks")

    def __init__(self, *, relay: GfsEnvelopeRelay) -> None:
        self._relay = relay
        self._stop = asyncio.Event()
        self._closing = asyncio.Event()
        self._queues: list[asyncio.Queue[_FanOutJob]] = [
            asyncio.Queue(maxsize=FAN_OUT_BACKLOG) for _ in range(FAN_OUT_WORKERS)
        ]
        #: Accepted-but-not-yet-sent items per key (the per-key share).
        self._pending: dict[str, int] = {}
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._closing.clear()
        self._tasks = [
            asyncio.create_task(self._worker(q), name=f"gfs-fanout-{i}")
            for i, q in enumerate(self._queues)
        ]

    async def stop(self) -> None:
        """Stop accepting, drain accepted items for up to
        :data:`FAN_OUT_STOP_DRAIN_S`, then stop the workers. Items that still
        could not go out are counted at WARNING (their seats catch up through
        space sync)."""
        self._closing.set()
        tasks, self._tasks = self._tasks, []
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(q.join() for q in self._queues)),
                    timeout=FAN_OUT_STOP_DRAIN_S,
                )
            except TimeoutError:
                pass
        self._stop.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        dropped = 0
        for q in self._queues:
            while not q.empty():
                q.get_nowait()
                q.task_done()
                dropped += 1
        self._pending.clear()
        if dropped:
            log.warning(
                "gfs.fan_out: %d accepted item(s) not fanned out before "
                "shutdown — their seats catch up through space sync",
                dropped,
            )

    async def wait_idle(self) -> None:
        """Wait until every accepted item has been fanned out (tests, and a
        graceful drain)."""
        await asyncio.gather(*(q.join() for q in self._queues))

    @staticmethod
    def _worker_index(key: str) -> int:
        digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % FAN_OUT_WORKERS

    def has_room(self, key: str) -> bool:
        """Whether :meth:`submit` would take an item for *key* now."""
        if self._closing.is_set() or not self._tasks:
            return False
        if self._pending.get(key, 0) >= FAN_OUT_BACKLOG_PER_KEY:
            return False
        return not self._queues[self._worker_index(key)].full()

    def submit(
        self,
        key: str,
        *,
        publisher_id: str,
        frame: dict,
        resolve: FanOutResolver,
        audience: tuple[str, str],
    ) -> bool:
        """Hand an accepted item to the workers. *resolve* finds its targets
        when the worker takes it; *audience* is what the other cluster nodes
        resolve to find their own sockets of those targets. ``False`` when
        stopped or full (the whole backlog, or *key*'s share of it)."""
        if self._closing.is_set() or not self._tasks:
            return False
        if self._pending.get(key, 0) >= FAN_OUT_BACKLOG_PER_KEY:
            return False
        job = _FanOutJob(
            key=key,
            publisher_id=publisher_id,
            frame=frame,
            resolve=resolve,
            audience=audience,
        )
        try:
            self._queues[self._worker_index(key)].put_nowait(job)
        except asyncio.QueueFull:
            return False
        self._pending[key] = self._pending.get(key, 0) + 1
        return True

    async def _worker(self, queue: asyncio.Queue[_FanOutJob]) -> None:
        while not self._stop.is_set():
            try:
                job = await asyncio.wait_for(queue.get(), timeout=FAN_OUT_IDLE_POLL_S)
            except TimeoutError:
                continue
            try:
                await self._fan_out(job)
            except Exception:
                log.exception("gfs.fan_out: fan-out failed")
            finally:
                left = self._pending.get(job.key, 1) - 1
                if left > 0:
                    self._pending[job.key] = left
                else:
                    self._pending.pop(job.key, None)
                queue.task_done()

    async def _fan_out(self, job: _FanOutJob) -> None:
        resolved, queue_ok = await job.resolve()
        # Never echo an item back to its publisher.
        targets = [t for t in resolved if t != job.publisher_id]
        if not targets:
            return
        reached = await self._relay.fan_out_relay(
            targets,
            queue_ok=queue_ok,
            frame=job.frame,
            audience=job.audience,
            skip=job.publisher_id,
        )
        log.debug("gfs.fan_out: reached=%d of %d", reached, len(targets))

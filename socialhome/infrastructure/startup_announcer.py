"""Tell paired households we're back — after the port is open, not before.

aiohttp opens the listen socket only once every ``on_startup`` hook has
returned. The startup steps that talk to peers (the post-restore
``URL_UPDATED`` fan-out, our ``proto_version`` announcement, the
capability re-sync after an upgrade) each ``await`` a federation send per
confirmed peer; a send to a household without a DataChannel yet posts an
RTC offer and then the envelope to its HTTPS inbox, each bounded by the
inbox timeout (10 s). One slow or silent peer therefore held the whole
household off the network for tens of seconds — and a peer answering our
offer found nobody listening.

This runs those steps in one background task started from ``on_startup``,
in order, each fail-soft, so ``on_startup`` returns at once and the site
starts listening. The ``_stop`` event follows the scheduler lifecycle
(``replay_cache_scheduler.py``): ``stop()`` lets the step in flight finish
and skips the rest, cancelling only after :data:`STOP_TIMEOUT_S`. Anything
a step could not deliver sits in the federation outbox as before.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

log = logging.getLogger(__name__)

#: One startup step: a label for the log and a zero-argument coroutine.
StartupStep = tuple[str, Callable[[], Awaitable[object]]]

#: How long ``stop()`` waits for the step in flight before cancelling it.
STOP_TIMEOUT_S = 5.0


class StartupAnnouncer:
    """Run the peer-facing startup steps off the startup path, in order."""

    __slots__ = ("_steps", "_stop", "_task")

    def __init__(self, steps: Sequence[StartupStep]) -> None:
        self._steps = tuple(steps)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the steps in the background. Idempotent; never blocks."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="startup-announcer")

    async def stop(self) -> None:
        """Skip the steps not started yet and wait for the one in flight."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=STOP_TIMEOUT_S)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        for label, step in self._steps:
            if self._stop.is_set():
                return
            try:
                await step()
            except Exception:
                log.warning("startup: %s failed", label, exc_info=True)


__all__ = ["STOP_TIMEOUT_S", "StartupAnnouncer", "StartupStep"]

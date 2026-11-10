"""Wait for a service's background tasks to finish.

Services keep their background tasks in a set (or dict) and drop each one
with ``task.add_done_callback(container.discard)``. That callback runs on a
LATER loop pass, and awaiting already-finished tasks never yields to the
loop — so ``while tasks: await gather(*tasks)`` spins forever when a task
has finished but not yet been discarded (the CI shard-4 hang, #1350).
:func:`wait_tasks_done` waits only for the unfinished ones.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable


async def wait_tasks_done(tasks: Callable[[], Iterable[asyncio.Future]]) -> None:
    """Wait until every task *tasks()* returns has finished, including ones
    started while waiting. *tasks* is re-read on every pass, so pass e.g.
    ``lambda: self._tasks`` or ``self._drains.values``."""
    while pending := [t for t in tasks() if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = ["wait_tasks_done"]

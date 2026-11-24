"""The daily sweep that pays owed content-key rotations (v_96).

A follower's leave or removal (not a ban) ends its seat without rotating
the space content key: the rotation is *owed* (``SpaceService.rotation_owed``,
one query, no state of its own). Any rotation pays it. Otherwise this
sweep does: once an hour it lists the spaces that owe and rotates, once,
each whose debt is at least ``min_age`` (24 h) old — so many follower
leaves in a day cost one rotation, not one each.

Only the owner or a seed-holding acting host rotates; that check, and a
re-read of the debt so a rotation that ran meanwhile is not doubled, live
in ``SpaceService.pay_owed_rotation``. One space failing never stops the
rest (logged at WARNING).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

log = logging.getLogger(__name__)


class OwedRotationPayer(Protocol):
    """What the sweep needs of the space service."""

    async def list_spaces_owing_rotation(self) -> list[tuple[str, datetime]]: ...

    async def pay_owed_rotation(
        self, space_id: str, *, min_age: timedelta = ...
    ) -> bool: ...


class OwedRotationScheduler:
    """Background task paying owed content-key rotations."""

    __slots__ = ("_interval", "_min_age", "_spaces", "_stop", "_task")

    def __init__(
        self,
        spaces: OwedRotationPayer,
        *,
        interval_seconds: float = 3600.0,
        min_age: timedelta = timedelta(hours=24),
    ) -> None:
        self._spaces = spaces
        self._interval = interval_seconds
        self._min_age = min_age
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for the task to exit. Idempotent."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.warning("owed-rotation sweep failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    async def tick(self) -> int:
        """One sweep. Returns how many spaces rotated. Exposed for tests."""
        cutoff = datetime.now(UTC) - self._min_age
        paid = 0
        for space_id, since in await self._spaces.list_spaces_owing_rotation():
            if since > cutoff:
                continue
            try:
                if await self._spaces.pay_owed_rotation(
                    space_id, min_age=self._min_age
                ):
                    paid += 1
            except Exception:
                log.warning(
                    "owed rotation for space %s failed — retried next sweep",
                    space_id,
                    exc_info=True,
                )
        return paid

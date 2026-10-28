"""Take a push back off the device once it no longer needs attention.

A push sits on the lock screen until someone swipes it away. When the thing
it is about was handled — the notification was read on any device, another
admin answered the "Needs you" request, the call was answered or declined —
this service withdraws it from every surface the push went to:

* **Web Push** — one small ``{"withdraw": [tags]}`` push per user and burst
  (:meth:`PushService.withdraw_from_user`); the service worker closes the
  notifications with those tags and shows nothing. Only push services whose
  browser tolerates a push that shows nothing get it (see
  :func:`~socialhome.services.push_service.tolerates_silent_push`).
* **Home Assistant Companion** — a ``clear_notification`` per tag through
  the platform adapter (``PushProvider.withdraw``).

What it costs (docs/protocol/push-relay.md, "Withdrawing a push"):

* Only tags this process actually pushed are withdrawn — a read of an
  in-app-only row, a silent repeat or something already withdrawn sends
  nothing. The record lives in memory (bounded per user, aged out after
  :data:`PUSHED_TTL_SECONDS`); after a restart older pushes stay on the
  device until they are swiped away or replaced.
* Withdraws coalesce per user for :data:`COALESCE_SECONDS`: reading a
  conversation, then the bell, then a page within a second or two is one
  Web Push per device, not three.

Lifecycle follows the scheduler pattern (``_stop: asyncio.Event``): one
loop flushes users whose window ran out; :meth:`stop` flushes everything
still pending before it returns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: How long withdraws for one user gather before they go out together.
COALESCE_SECONDS: float = 2.0

#: The most pushed tags remembered per user (oldest forgotten first).
MAX_PUSHED_PER_USER: int = 200

#: How long a pushed tag stays withdrawable. A push older than this is left
#: alone — the device most likely dropped or replaced it long ago.
PUSHED_TTL_SECONDS: float = 3 * 24 * 3600.0

#: The most tags one flush clears on Home Assistant: the Companion
#: ``clear_notification`` takes a single tag, so each is its own call.
MAX_HA_CLEARS_PER_FLUSH: int = 20


class PushWithdrawService:
    """Remember what was pushed; withdraw it once it was handled."""

    __slots__ = (
        "_adapter",
        "_due",
        "_pending",
        "_push",
        "_pushed",
        "_stop",
        "_task",
        "_users",
        "_wake",
        "_window",
    )

    def __init__(
        self,
        user_repo: AbstractUserRepo,
        *,
        window_seconds: float = COALESCE_SECONDS,
    ) -> None:
        self._users = user_repo
        self._window = window_seconds
        self._push: Any = None  # attach_push_service(PushService)
        self._adapter: Any = None  # attach_platform_adapter(PlatformAdapter)
        #: user_id → {tag: monotonic time it was pushed}, oldest first.
        self._pushed: dict[str, OrderedDict[str, float]] = {}
        #: user_id → tags waiting for the user's window to run out.
        self._pending: dict[str, dict[str, None]] = {}
        #: user_id → loop time the user's pending tags go out.
        self._due: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def attach_push_service(self, push_service) -> None:
        self._push = push_service

    def attach_platform_adapter(self, adapter) -> None:
        self._adapter = adapter

    # ── What was pushed ──────────────────────────────────────────────

    def note_pushed(self, user_id: str, tag: str) -> None:
        """*tag* just went out to *user_id*'s devices. A withdraw of the
        same tag still waiting would take the fresh push down — drop it."""
        pending = self._pending.get(user_id)
        if pending is not None:
            pending.pop(tag, None)
        pushed = self._pushed.setdefault(user_id, OrderedDict())
        pushed[tag] = time.monotonic()
        pushed.move_to_end(tag)
        while len(pushed) > MAX_PUSHED_PER_USER:
            pushed.popitem(last=False)

    def _take_pushed(self, user_id: str, tags: Iterable[str]) -> list[str]:
        """The tags of *tags* still on *user_id*'s devices as far as we
        know — forgotten here, they are withdrawn once."""
        pushed = self._pushed.get(user_id)
        if not pushed:
            return []
        cutoff = time.monotonic() - PUSHED_TTL_SECONDS
        out: list[str] = []
        for tag in dict.fromkeys(tags):
            stamp = pushed.pop(tag, None)
            if stamp is not None and stamp >= cutoff:
                out.append(tag)
        if not pushed:
            self._pushed.pop(user_id, None)
        return out

    # ── Withdrawing ──────────────────────────────────────────────────

    def withdraw(self, user_id: str, tags: Iterable[str]) -> list[str]:
        """Take the pushes *tags* off *user_id*'s devices, within
        :data:`COALESCE_SECONDS`. Returns the tags that will be withdrawn
        (those actually pushed); nothing goes out for the rest."""
        taken = self._take_pushed(user_id, tags)
        if not taken:
            return []
        pending = self._pending.setdefault(user_id, {})
        for tag in taken:
            pending[tag] = None
        if user_id not in self._due:
            self._due[user_id] = asyncio.get_running_loop().time() + self._window
            self._wake.set()
        return taken

    async def flush(self, user_id: str) -> None:
        """Send *user_id*'s pending withdraws now. Exposed for tests."""
        self._due.pop(user_id, None)
        tags = list(self._pending.pop(user_id, {}))
        if not tags:
            return
        if self._push is not None:
            try:
                await self._push.withdraw_from_user(user_id, tags)
            except Exception as exc:
                log.warning("push withdraw (web) failed: %s", exc, exc_info=True)
        if self._adapter is not None:
            user = await self._users.get_by_user_id(user_id)
            if user is not None:
                try:
                    await self._adapter.withdraw_push(
                        user, tags[:MAX_HA_CLEARS_PER_FLUSH]
                    )
                except Exception as exc:
                    log.warning(
                        "push withdraw (platform) failed: %s", exc, exc_info=True
                    )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the flush loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop; withdraws still waiting go out first."""
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            now = loop.time()
            for user_id in [u for u, at in self._due.items() if at <= now]:
                await self.flush(user_id)
            timeout = (
                max(0.0, min(self._due.values()) - loop.time()) if self._due else None
            )
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                continue
        for user_id in list(self._due):
            await self.flush(user_id)

"""Does this space need sync? — the §25.6 sync status a member sees.

For every space this household does not host, the households acting as its
host here (the owner, or the trusted inviting admin —
``federation/host_equivalence.acting_hosts``) answer our periodic
``SPACE_SYNC_BEGIN``. Each answer's outcome is kept on that household's
``space_instances`` row (migration 0150, :class:`SpaceSyncResult`):

* ``ok`` / ``unclean`` — the receiver's end-of-stream verdict
  (:class:`SpaceSyncComplete`; the quiet sentinel of an up-to-date space is
  a clean one);
* ``deferred`` — the host refused for now or the stream broke off
  (:class:`SpaceSyncDeferred`);
* ``unreachable`` — the scheduler's host-silent verdict
  (:meth:`host_unreachable`, after the round's answer wait).

Writes are rare: a result is written when it changes, an ``ok`` refreshes its
time at most every :data:`OK_REFRESH_S` — never per chunk.

:meth:`status` turns the rows into one state at read time
(:func:`~socialhome.domain.space_sync_status.sync_state`), plus how many of
our own items of the space still wait in the outbox. The space's owner
household is always up to date. When the computed state changes after an
outcome or a stream start, :class:`SpaceSyncStateChanged` goes on the bus —
the realtime layer sends the members a ``space.sync_state`` frame. Nothing
here goes on the wire to another household.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.events import (
    SpaceSyncComplete,
    SpaceSyncDeferred,
    SpaceSyncStateChanged,
)
from ..domain.space_sync_page import SyncPagePosition, state_with_page
from ..domain.space_sync_status import (
    SpaceSyncOutcome,
    SpaceSyncResult,
    SpaceSyncState,
    SpaceSyncStatus,
    parse_utc,
    sync_state,
)
from .bus_publisher import BusPublisherMixin

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)

#: An ``ok`` refreshes its stored time at most this often per (space,
#: host) — one write per 30-minute round, never one per stream when several
#: land close together ("Sync now" next to a periodic round).
OK_REFRESH_S: float = 15 * 60

#: Spaces whose last computed state is remembered (frame on change).
MAX_TRACKED: int = 4096


class _Spaces(Protocol):
    async def get(self, space_id: str) -> Space | None: ...


class _Outcomes(Protocol):
    async def record_outcome(
        self,
        space_id: str,
        instance_id: str,
        result: SpaceSyncResult,
        *,
        ok_at: str | None = None,
    ) -> None: ...

    async def outcomes(
        self, space_id: str, instance_ids: list[str]
    ) -> list[SpaceSyncOutcome]: ...


class _Outbox(Protocol):
    async def count_pending_for_space(self, space_id: str) -> int: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SpaceSyncStatusService(BusPublisherMixin):
    """Records each acting host's sync outcome; answers the sync status."""

    __slots__ = (
        "_bus",
        "_clock",
        "_hosts",
        "_last_state",
        "_outbox",
        "_outcomes",
        "_own_instance_id",
        "_pages",
        "_receiving",
        "_space_repo",
        "_tasks",
    )

    def __init__(
        self,
        *,
        bus: EventBus | None,
        space_repo: _Spaces,
        outcomes: _Outcomes,
        outbox: _Outbox,
        hosts: Callable[[Space], Awaitable[list[str]]],
        own_instance_id: str,
        receiving: Callable[[str, str], bool] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo
        self._outcomes = outcomes
        self._outbox = outbox
        #: The households acting as a space's host here, owner first.
        self._hosts = hosts
        self._own_instance_id = own_instance_id
        #: ``(space_id, ignore_sync_id) ->`` whether a stream of the space is
        #: arriving here now (the session manager). ``None``: never.
        self._receiving = receiving
        self._clock = clock
        #: v_95 — ``(space, acting hosts) ->`` what the newest signed sync
        #: page says about our copy (``SyncPageService.position``). ``None``:
        #: the Phase-1 rules alone.
        self._pages: (
            Callable[[Space, list[str]], Awaitable[SyncPagePosition | None]] | None
        ) = None
        #: space → the state last computed after an outcome (frame on change).
        self._last_state: OrderedDict[str, SpaceSyncState] = OrderedDict()
        #: Outcomes being recorded (:meth:`_spawn`).
        self._tasks: set[asyncio.Task] = set()

    def attach_pages(
        self,
        position: Callable[[Space, list[str]], Awaitable[SyncPagePosition | None]],
    ) -> None:
        """Wire the v_95 sync pages: the state also says "up to date as of
        page N" (even while the host is offline) or "behind: a newer page
        exists" (``domain/space_sync_page.state_with_page``)."""
        self._pages = position

    async def recheck(self, space_id: str) -> None:
        """Recompute the space's state (the page held changed) and send the
        frame when it moved. Fail-soft."""
        try:
            space = await self._space_repo.get(space_id)
            if space is None or space.owner_instance_id == self._own_instance_id:
                return
            await self._recheck(space)
        except Exception:
            log.exception("sync status: recheck of space %s failed", space_id)

    def wire(self) -> None:
        """Subscribe to the outcomes on the bus."""
        if self._bus is None:
            return
        self._bus.subscribe(SpaceSyncComplete, self._on_complete)
        self._bus.subscribe(SpaceSyncDeferred, self._on_deferred)

    # ─── Read ──────────────────────────────────────────────────────────

    async def status(self, space_id: str) -> SpaceSyncStatus | None:
        """The space's sync status; ``None`` for an unknown space. The
        caller decides who may see it (members, never followers)."""
        space = await self._space_repo.get(space_id)
        if space is None:
            return None
        return await self._status_of(space)

    async def _status_of(
        self, space: Space, *, ignore_sync_id: str = ""
    ) -> SpaceSyncStatus:
        waiting = await self._outbox.count_pending_for_space(space.id)
        if space.owner_instance_id == self._own_instance_id:
            return SpaceSyncStatus(
                state=SpaceSyncState.UP_TO_DATE,
                last_ok_at=None,
                waiting_to_send=waiting,
            )
        hosts = await self._hosts(space)
        outcomes = await self._outcomes.outcomes(space.id, hosts)
        receiving = (
            self._receiving(space.id, ignore_sync_id)
            if self._receiving is not None
            else False
        )
        state, last_ok = sync_state(outcomes, receiving=receiving, now=self._clock())
        page = await self._pages(space, hosts) if self._pages is not None else None
        return SpaceSyncStatus(
            state=state_with_page(state, page),
            last_ok_at=last_ok,
            waiting_to_send=waiting,
            page=page.to_wire() if page is not None else None,
        )

    # ─── Outcomes ─────────────────────────────────────────────────────

    async def _on_complete(self, event: SpaceSyncComplete) -> None:
        result = SpaceSyncResult.OK if event.clean else SpaceSyncResult.UNCLEAN
        self._spawn(
            self.record(
                event.space_id,
                event.from_instance,
                result,
                ignore_sync_id=event.sync_id,
            )
        )

    async def _on_deferred(self, event: SpaceSyncDeferred) -> None:
        self._spawn(
            self.record(
                event.space_id,
                event.provider_instance_id,
                SpaceSyncResult.DEFERRED,
                ignore_sync_id=event.sync_id,
            )
        )

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        """Record in a task of our own, never in the publisher's: the
        receiver publishes the end of a stream from the session's drain
        task, and the federation service's own handler of the same event
        closes that session — which cancels the drain task while the bus is
        still calling handlers. Inline, our write died with it, silently
        (``CancelledError`` is no ``Exception``)."""
        task = asyncio.create_task(work, name="space-sync-status")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """Wait for the outcomes being recorded (tests; shutdown)."""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def host_unreachable(self, space_id: str, hosts: Iterable[str]) -> None:
        """The scheduler waited a round's full while for *hosts* (the
        households acting as the space's host) and none answered."""
        for host in hosts:
            await self.record(space_id, host, SpaceSyncResult.UNREACHABLE)

    async def stream_started(self, space_id: str, provider: str) -> None:
        """A stream of the space from *provider* began arriving here: a
        member that was behind (or never synced) is now catching up."""
        space = await self._space_repo.get(space_id)
        if space is None or space.owner_instance_id == self._own_instance_id:
            return
        if provider not in await self._hosts(space):
            return
        await self._recheck(space)

    async def record(
        self,
        space_id: str,
        provider: str,
        result: SpaceSyncResult,
        *,
        ignore_sync_id: str = "",
    ) -> None:
        """Keep *provider*'s outcome when it acts as the space's host here,
        then recompute the state. Fail-soft: a lost outcome only makes the
        status older."""
        try:
            space = await self._space_repo.get(space_id)
            if (
                space is None
                or not provider
                or space.owner_instance_id == self._own_instance_id
                or provider not in await self._hosts(space)
            ):
                return
            await self._write(space_id, provider, result)
            await self._recheck(space, ignore_sync_id=ignore_sync_id)
        except Exception:
            log.exception(
                "sync status: recording %s from %s for space %s failed",
                result,
                provider,
                space_id,
            )

    async def _write(
        self, space_id: str, provider: str, result: SpaceSyncResult
    ) -> None:
        """Write only what changes the stored row — read first (one small
        SELECT per answer, once per round): never a write the row already
        says, and an ``ok`` refreshes its time at most every
        :data:`OK_REFRESH_S`. The row decides, not memory: a household whose
        seat row arrives after its first answer still gets the next one."""
        now = self._clock()
        held = await self._outcomes.outcomes(space_id, [provider])
        if not held:
            return  # no seat row for that household: nothing to keep
        stored = held[0]
        if stored.result is result:
            if result is not SpaceSyncResult.OK:
                return  # unchanged
            last_ok = parse_utc(stored.ok_at)
            if last_ok is not None and (now - last_ok).total_seconds() < OK_REFRESH_S:
                return  # refreshed this round already
        ok_at = now.isoformat() if result is SpaceSyncResult.OK else None
        await self._outcomes.record_outcome(space_id, provider, result, ok_at=ok_at)

    async def _recheck(self, space: Space, *, ignore_sync_id: str = "") -> None:
        status = await self._status_of(space, ignore_sync_id=ignore_sync_id)
        if self._last_state.get(space.id) is status.state:
            return
        self._last_state[space.id] = status.state
        self._last_state.move_to_end(space.id)
        while len(self._last_state) > MAX_TRACKED:
            self._last_state.popitem(last=False)
        await self._emit(
            SpaceSyncStateChanged(space_id=space.id, sync=status.to_wire())
        )


__all__ = ["OK_REFRESH_S", "SpaceSyncStatusService"]

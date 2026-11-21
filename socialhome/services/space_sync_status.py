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

Writes are rare: a result is written when it changes, and the same result
refreshes its time (and an ``ok`` its ``ok`` time) at most every
:data:`RESULT_REFRESH_S` — never per chunk. A last answer older than two
rounds no longer counts as the host answering (``sync_state``).

:meth:`status` turns the rows into one state at read time
(:func:`~socialhome.domain.space_sync_status.sync_state`), plus how many of
our own items of the space still wait in the outbox. The space's owner
household is always up to date. When the computed state or the waiting count
changes after an outcome, a stream start or a change of the space's outbox
rows (:meth:`SpaceSyncStatusService.outbox_changed`),
:class:`SpaceSyncStateChanged` goes on the bus — the realtime layer sends
the members a ``space.sync_state`` frame. Nothing
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

#: The same result refreshes its stored time at most this often per (space,
#: host) — one write per 30-minute round, never one per stream when several
#: land close together ("Sync now" next to a periodic round).
RESULT_REFRESH_S: float = 15 * 60

#: Spaces whose last framed status is remembered (frame on change).
MAX_TRACKED: int = 4096

#: A space's recheck holds it this long after it ran; a trigger meanwhile
#: runs once at the end of that wait. Frames go out at most once per this
#: while per space, however busy its outbox or
#: its streams (a broadcast settling 500 rows reads the count a few times,
#: not 500). Fixed, never random.
RECHECK_SPACING_S: float = 2.0

#: How long :meth:`SpaceSyncStatusService.stop` waits for outcomes still
#: being recorded before cancelling them.
STOP_TIMEOUT_S: float = 5.0


class _Spaces(Protocol):
    async def get(self, space_id: str) -> Space | None: ...


class _Outcomes(Protocol):
    async def record_outcome(
        self,
        space_id: str,
        instance_id: str,
        result: SpaceSyncResult,
        *,
        at: str,
        ok_at: str | None = None,
    ) -> None: ...

    async def outcomes(
        self, space_id: str, instance_ids: list[str]
    ) -> list[SpaceSyncOutcome]: ...


class _Outbox(Protocol):
    async def count_pending_for_space(self, space_id: str) -> int: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Recheck:
    """A space's one recheck in flight: whether another came due while it
    ran, and the ending stream that one must not count."""

    __slots__ = ("due", "ignore_sync_id")

    def __init__(self, ignore_sync_id: str) -> None:
        self.due = False
        self.ignore_sync_id = ignore_sync_id


class SpaceSyncStatusService(BusPublisherMixin):
    """Records each acting host's sync outcome; answers the sync status."""

    __slots__ = (
        "_bus",
        "_clock",
        "_hosts",
        "_last_sent",
        "_outbox",
        "_outcomes",
        "_own_instance_id",
        "_pages",
        "_receiving",
        "_recheck_spacing_s",
        "_rechecks",
        "_space_repo",
        "_stop",
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
        receiving: Callable[[str, list[str], str], bool] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        recheck_spacing_s: float = RECHECK_SPACING_S,
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo
        self._outcomes = outcomes
        self._outbox = outbox
        #: The households acting as a space's host here, owner first.
        self._hosts = hosts
        self._own_instance_id = own_instance_id
        #: ``(space_id, providers, ignore_sync_id) ->`` whether a stream of
        #: the space from one of *providers* (its acting hosts) is arriving
        #: here now (the session manager). ``None``: never.
        self._receiving = receiving
        self._clock = clock
        #: space → the (state, waiting count) last framed (frame on change).
        self._last_sent: OrderedDict[str, tuple[SpaceSyncState, int]] = OrderedDict()
        #: space → its one recheck in flight (:meth:`_request_recheck`).
        self._rechecks: dict[str, _Recheck] = {}
        self._recheck_spacing_s = recheck_spacing_s
        #: Set by :meth:`stop`: no new work, a spacing wait ends.
        self._stop = asyncio.Event()
        #: v_95 — ``(space, acting hosts) ->`` what the newest signed sync
        #: page says about our copy (``SyncPageService.position``). ``None``:
        #: the Phase-1 rules alone.
        self._pages: (
            Callable[[Space, list[str]], Awaitable[SyncPagePosition | None]] | None
        ) = None
        #: Outcomes being recorded and rechecks running (:meth:`_spawn`).
        self._tasks: set[asyncio.Task] = set()

    def attach_pages(
        self,
        position: Callable[[Space, list[str]], Awaitable[SyncPagePosition | None]],
    ) -> None:
        """Wire the v_95 sync pages: the state also says "up to date as of
        page N" (even while the host is offline) or "behind: a newer page
        exists" (``domain/space_sync_page.state_with_page``)."""
        self._pages = position

    async def page_changed(self, space_id: str) -> None:
        """The sync page held for the space changed (a new page stored, a
        root check done): the one coalesced recheck
        (:meth:`_request_recheck`)."""
        self._request_recheck(space_id)

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
            self._receiving(space.id, hosts, ignore_sync_id)
            if self._receiving is not None and hosts
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
        (``CancelledError`` is no ``Exception``). Nothing new once stopped:
        the database is about to close."""
        if self._stop.is_set():
            work.close()
            return
        task = asyncio.create_task(work, name="space-sync-status")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """Wait for the outcomes being recorded and the rechecks (tests)."""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Shutdown, before the database closes: take no new work, let the
        outcomes being recorded finish (a due recheck's spacing wait ends at
        once and it is not run), cancel whatever is still running after
        *timeout*."""
        self._stop.set()
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return
        _done, late = await asyncio.wait(pending, timeout=timeout)
        for task in late:
            task.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)

    async def host_unreachable(self, space_id: str, hosts: Iterable[str]) -> None:
        """The scheduler waited a round's full while for *hosts* (the
        households acting as the space's host) and none answered — for any
        space one of our people holds a member seat in, a household holding
        the roster authority included: it is not the owner and can itself
        be behind, so its members read "host offline" while the owner is
        down."""
        for host in hosts:
            await self.record(space_id, host, SpaceSyncResult.UNREACHABLE)

    def outbox_changed(self, space_id: str) -> None:
        """Rows of *space_id*'s outbox were queued, delivered or dropped
        (the federation service, the outbox processor): frame the waiting
        count if it moved. Never blocks the caller, never reads per row
        (:meth:`_request_recheck`)."""
        self._request_recheck(space_id)

    def stream_started(self, space_id: str, provider: str) -> None:
        """A stream of the space from *provider* began arriving here: a
        member that was behind (or never synced) is now catching up. Called
        from the receiver's chunk path: returns at once, the reads happen in
        a task of our own."""
        self._spawn(self._on_stream_started(space_id, provider))

    async def _on_stream_started(self, space_id: str, provider: str) -> None:
        try:
            space = await self._space_repo.get(space_id)
            if space is None or space.owner_instance_id == self._own_instance_id:
                return
            if provider in await self._hosts(space):
                self._request_recheck(space_id)
        except Exception:
            log.exception("sync status: noting the stream of %s failed", space_id)

    def _request_recheck(self, space_id: str, *, ignore_sync_id: str = "") -> None:
        """THE one path every trigger (an outcome, a stream start, an
        outbox change) takes to the frame: at most one recheck runs per
        space, so frames go out in order and never an older view after a
        newer one. A trigger while one runs marks it due (keeping the
        stream that is ending, *ignore_sync_id*); a recheck holds the space
        for :data:`RECHECK_SPACING_S` after it ran, so the due re-run comes
        that long after the last."""
        if not space_id or self._stop.is_set():
            return
        running = self._rechecks.get(space_id)
        if running is not None:
            running.due = True
            if ignore_sync_id:
                running.ignore_sync_id = ignore_sync_id
            return
        slot = self._rechecks[space_id] = _Recheck(ignore_sync_id)
        self._spawn(self._rechecks_of(space_id, slot))

    async def _rechecks_of(self, space_id: str, slot: _Recheck) -> None:
        try:
            while True:
                ignore, slot.ignore_sync_id = slot.ignore_sync_id, ""
                space = await self._space_repo.get(space_id)
                if space is not None:
                    await self._recheck(space, ignore_sync_id=ignore)
                # Held for the spacing after every run: a trigger meanwhile
                # waits for its end, so frames are at least that far apart.
                if self._recheck_spacing_s > 0:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self._recheck_spacing_s
                        )
                        return  # shutting down
                    except TimeoutError:
                        pass
                if not slot.due or self._stop.is_set():
                    return
                slot.due = False
        except Exception:
            log.exception("sync status: rechecking space %s failed", space_id)
        finally:
            if self._rechecks.get(space_id) is slot:
                del self._rechecks[space_id]

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
            self._request_recheck(space_id, ignore_sync_id=ignore_sync_id)
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
        says, and the same result refreshes its time at most every
        :data:`RESULT_REFRESH_S`. The row decides, not memory: a household
        whose seat row arrives after its first answer still gets the next
        one."""
        now = self._clock()
        held = await self._outcomes.outcomes(space_id, [provider])
        if not held:
            return  # no seat row for that household: nothing to keep
        stored = held[0]
        if stored.result is result:
            last = parse_utc(stored.result_at)
            if last is None and result is SpaceSyncResult.OK:
                last = parse_utc(stored.ok_at)
            if last is not None and (now - last).total_seconds() < RESULT_REFRESH_S:
                return  # refreshed this round already
        stamp = now.isoformat()
        ok_at = stamp if result is SpaceSyncResult.OK else None
        await self._outcomes.record_outcome(
            space_id, provider, result, at=stamp, ok_at=ok_at
        )

    async def _recheck(self, space: Space, *, ignore_sync_id: str = "") -> None:
        status = await self._status_of(space, ignore_sync_id=ignore_sync_id)
        sent = (status.state, status.waiting_to_send)
        if self._last_sent.get(space.id) == sent:
            return
        self._last_sent[space.id] = sent
        self._last_sent.move_to_end(space.id)
        while len(self._last_sent) > MAX_TRACKED:
            self._last_sent.popitem(last=False)
        await self._emit(
            SpaceSyncStateChanged(space_id=space.id, sync=status.to_wire())
        )


__all__ = ["RESULT_REFRESH_S", "SpaceSyncStatusService"]

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

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from ..domain.events import (
    SpaceSyncComplete,
    SpaceSyncDeferred,
    SpaceSyncStateChanged,
)
from ..domain.space_sync_status import (
    SpaceSyncOutcome,
    SpaceSyncResult,
    SpaceSyncState,
    SpaceSyncStatus,
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

#: Spaces (and (space, host) pairs) whose last state / write is remembered.
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
        "_receiving",
        "_space_repo",
        "_written",
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
        #: space → the state last computed after an outcome (frame on change).
        self._last_state: OrderedDict[str, SpaceSyncState] = OrderedDict()
        #: (space, host) → (result last written, when an ``ok`` was written).
        self._written: OrderedDict[
            tuple[str, str], tuple[SpaceSyncResult, datetime | None]
        ] = OrderedDict()

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
        return SpaceSyncStatus(state=state, last_ok_at=last_ok, waiting_to_send=waiting)

    # ─── Outcomes ─────────────────────────────────────────────────────

    async def _on_complete(self, event: SpaceSyncComplete) -> None:
        result = SpaceSyncResult.OK if event.clean else SpaceSyncResult.UNCLEAN
        await self.record(
            event.space_id, event.from_instance, result, ignore_sync_id=event.sync_id
        )

    async def _on_deferred(self, event: SpaceSyncDeferred) -> None:
        await self.record(
            event.space_id,
            event.provider_instance_id,
            SpaceSyncResult.DEFERRED,
            ignore_sync_id=event.sync_id,
        )

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
        now = self._clock()
        key = (space_id, provider)
        held = self._written.get(key)
        if held is not None and held[0] is result:
            if result is not SpaceSyncResult.OK:
                return  # unchanged
            if held[1] is not None and (now - held[1]).total_seconds() < OK_REFRESH_S:
                return  # refreshed this round already
        ok_at = now.isoformat() if result is SpaceSyncResult.OK else None
        await self._outcomes.record_outcome(space_id, provider, result, ok_at=ok_at)
        ok_written = now if ok_at is not None else (held[1] if held else None)
        self._written[key] = (result, ok_written)
        self._written.move_to_end(key)
        while len(self._written) > MAX_TRACKED:
            self._written.popitem(last=False)

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

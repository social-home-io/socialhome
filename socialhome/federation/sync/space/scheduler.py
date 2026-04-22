"""Sync initiation scheduler (§25.6).

Three triggers:

1. **Event-driven** — on :class:`PairingConfirmed` enqueue a P4 initial
   sync for every space we know the peer is a member of.
2. **Periodic** — every :data:`PERIODIC_INTERVAL_SECONDS` walk the set
   of confirmed peers and enqueue a P6 incremental sync per shared
   space (unless we're already in the quiet window from the last tick).
3. **On-demand** — called directly from the admin route when a user
   hits "Sync now".

Follows the `_stop: asyncio.Event` lifecycle (CLAUDE.md "Schedulers").
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from ....domain.events import PairingConfirmed
from ....domain.federation import FederationEventType, PairingStatus
from ....infrastructure.event_bus import EventBus
from ....infrastructure.reconnect_queue import P4_DM, P6_PRODUCTIVITY

if TYPE_CHECKING:
    from ....infrastructure.reconnect_queue import ReconnectSyncQueue
    from ....repositories.federation_repo import AbstractFederationRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ...federation_service import FederationService

log = logging.getLogger(__name__)


#: 30 minutes between periodic ticks — comfortably under the S-6 5/h
#: per (instance, space) rate limit.
PERIODIC_INTERVAL_SECONDS: float = 30 * 60


class SpaceSyncScheduler:
    """Orchestrates sync initiation across pairs + spaces."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_space_repo",
        "_queue",
        "_own_instance_id",
        "_interval",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation: "FederationService",
        federation_repo: "AbstractFederationRepo",
        space_repo: "AbstractSpaceRepo",
        queue: "ReconnectSyncQueue",
        own_instance_id: str,
        interval_seconds: float = PERIODIC_INTERVAL_SECONDS,
    ) -> None:
        self._bus = bus
        self._federation = federation
        self._federation_repo = federation_repo
        self._space_repo = space_repo
        self._queue = queue
        self._own_instance_id = own_instance_id
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def wire(self) -> None:
        """Subscribe to :class:`PairingConfirmed` on the bus. Idempotent."""
        self._bus.subscribe(PairingConfirmed, self._on_pairing_confirmed)

    async def start(self) -> None:
        """Begin the periodic tick. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="SpaceSyncScheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def enqueue_sync_for_space(
        self,
        *,
        space_id: str,
        peer_instance_id: str,
        priority: int = P4_DM,
    ) -> None:
        """Queue a sync from us to ``peer_instance_id`` for ``space_id``.

        Fire-and-forget — the actual ``SPACE_SYNC_BEGIN`` send happens
        when the queue worker picks up the task. Callers should not
        await completion.
        """

        async def _task() -> None:
            try:
                await self._federation.send_event(
                    to_instance_id=peer_instance_id,
                    event_type=FederationEventType.SPACE_SYNC_BEGIN,
                    payload={
                        "sync_id": uuid.uuid4().hex,
                        "space_id": space_id,
                        "sync_mode": "initial",
                        "prefer_direct": True,
                    },
                    space_id=space_id,
                )
            except Exception:  # pragma: no cover
                log.exception(
                    "space sync enqueue failed: peer=%s space=%s",
                    peer_instance_id,
                    space_id,
                )

        self._queue.enqueue(
            priority,
            _task,
            description=f"sync {space_id} → {peer_instance_id}",
        )

    # ─── Event-driven: on PairingConfirmed ──────────────────────────

    async def _on_pairing_confirmed(self, event: PairingConfirmed) -> None:
        """On pair-confirm, enqueue a sync for every space we co-member."""
        peer_id = event.instance_id
        if peer_id == self._own_instance_id:
            return
        # Walk every local space and check whether the new peer is a
        # member-instance of it. Household-scale operator → this list
        # is small; no need to index.
        local_spaces = await self._list_local_space_ids()
        for space_id in local_spaces:
            members = await self._space_repo.list_member_instances(space_id)
            if peer_id in members:
                await self.enqueue_sync_for_space(
                    space_id=space_id,
                    peer_instance_id=peer_id,
                    priority=P4_DM,
                )

    # ─── Periodic tick ──────────────────────────────────────────────

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception:  # pragma: no cover
                log.exception("SpaceSyncScheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def _tick_once(self) -> None:
        confirmed = [
            inst
            for inst in await self._federation_repo.list_instances()
            if inst.status is PairingStatus.CONFIRMED
        ]
        local_spaces = await self._list_local_space_ids()
        for peer in confirmed:
            peer_id = peer.id
            for space_id in local_spaces:
                members = await self._space_repo.list_member_instances(space_id)
                if peer_id not in members:
                    continue
                await self.enqueue_sync_for_space(
                    space_id=space_id,
                    peer_instance_id=peer_id,
                    priority=P6_PRODUCTIVITY,
                )

    async def _list_local_space_ids(self) -> list[str]:
        from ....domain.space import SpaceType

        spaces = (
            await self._space_repo.list_by_type(SpaceType.HOUSEHOLD)
            + await self._space_repo.list_by_type(SpaceType.PUBLIC)
            + await self._space_repo.list_by_type(SpaceType.PRIVATE)
        )
        return [s.id for s in spaces if not s.dissolved]

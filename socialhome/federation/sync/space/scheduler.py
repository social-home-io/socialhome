"""Sync initiation scheduler (§25.6).

Triggers:

1. **Event-driven** — on :class:`PairingConfirmed` enqueue a P4 initial
   sync for every space we know the peer is a member of.
2. **Periodic** — every :data:`PERIODIC_INTERVAL_SECONDS` walk the set
   of confirmed peers and enqueue a P6 incremental sync per shared
   space (unless we're already in the quiet window from the last tick).
3. **On-demand** — called directly from the admin route when a user
   hits "Sync now".
4. **Mesh catch-up** — a space whose host is reachable only over the
   mesh is invisible to triggers 1 and 2 (both walk CONFIRMED peers),
   so a restart would leave it permanently un-synced: the requester's
   ``sync_id`` and the host's route cache both live in RAM. Startup and
   every periodic tick re-issue ``SPACE_SYNC_BEGIN`` for any such space
   that has not yet been seen to complete (#648).
5. **Deferred** — a provider caps concurrent syncs per household (S-6),
   so a household that restarts and asks one host for more spaces than
   the cap gets ``too_many_sessions`` for the rest. The requester's
   federation service turns that into :class:`SpaceSyncDeferred` and the
   scheduler asks again after :data:`DEFERRED_SYNC_RETRY_S` (growing,
   at most :data:`MAX_DEFERRED_SYNC_RETRIES` until that space completes),
   in the mode the refused BEGIN asked for — a periodic sync stays
   ``"incremental"``.
6. **Authority epoch echo** (v_46) — a BEGIN to a space's owner carries
   the ``authority_epoch_echo`` the attached builder returns
   (:meth:`attach_authority_echo`), and :class:`SpaceAuthorityEchoDue`
   sends one to the owner right away.
7. **Mesh-only host announcement** — the same mesh sweep hands each host
   we reach only over the mesh to the attached announcer
   (:meth:`attach_mesh_announce`) once per process, so it learns our
   version + identity key and can deliver our writer cert (migration 0078).
8. **Per-provider slots** — every BEGIN this scheduler sends holds one of
   the provider's :data:`MAX_ACTIVE_SESSIONS_PER_INSTANCE` slots until its
   ``SpaceSyncComplete`` (or deferral, or :data:`INFLIGHT_BEGIN_TTL_S`). A
   sync asked for while all are held waits — one entry per space, the
   stronger mode and the more urgent priority kept — and goes when one
   frees, so a periodic tick over many shared spaces is never refused.

Follows the `_stop: asyncio.Event` lifecycle (CLAUDE.md "Schedulers").
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ....domain.events import (
    PairingConfirmed,
    SpaceAuthorityEchoDue,
    SpaceSyncComplete,
    SpaceSyncDeferred,
)
from ....domain.federation import FederationEventType, PairingStatus
from ....domain.space import Space, SpaceRole, SpaceType
from ....infrastructure.event_bus import EventBus
from ....infrastructure.reconnect_queue import P1_SECURITY, P4_DM, P6_PRODUCTIVITY
from ...host_equivalence import acts_as_host
from ...sync_manager import MAX_ACTIVE_SESSIONS_PER_INSTANCE

if TYPE_CHECKING:
    from ....infrastructure.reconnect_queue import ReconnectSyncQueue
    from ....repositories.federation_repo import AbstractFederationRepo
    from ....repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.space_sync_watermark_repo import (
        AbstractSpaceSyncWatermarkRepo,
    )
    from ...federation_service import FederationService
    from ...sync_manager import SyncSessionManager

log = logging.getLogger(__name__)


#: 30 minutes between periodic ticks — comfortably under the S-6 5/h
#: per (instance, space) rate limit.
PERIODIC_INTERVAL_SECONDS: float = 30 * 60

#: Idle time (no chunk moved — ``SyncSessionRecord.last_activity``) past
#: which an in-memory sync session is considered abandoned and reaped by
#: the periodic tick (§25.6 backstop for the never-emitted
#: ``SPACE_SYNC_COMPLETE``). Idle, not age: a big stream may run longer
#: than this and is kept while it makes progress. With the periodic tick a
#: leaked session lives at most ~PERIODIC_INTERVAL_SECONDS + this TTL.
STALE_SESSION_TTL_SECONDS: float = 1800.0

#: Delay before the startup mesh catch-up sweep. Long enough for the
#: ``INSTANCE_CAPABILITIES_UPDATED`` exchange to have settled (so
#: ``is_confirmed_peer`` and ``peer_supports`` answer truthfully) and for
#: the mesh's confirmed-peer set to be reachable, short enough that a
#: restarted household isn't waiting a full periodic interval for the
#: metadata it is missing.
STARTUP_MESH_CATCHUP_DELAY_SECONDS: float = 45.0

#: Cap on *delivered* mesh catch-up BEGINs per (space, host) per process
#: lifetime. The host enforces 5/h per (instance, space) itself; this keeps a
#: crash-looping host from burning that budget and starving a real sync later.
#: Only BEGINs that actually shipped count — a ``no_route`` never reached the
#: host and so never touched its budget (#648).
MAX_MESH_CATCHUP_ATTEMPTS: int = 3

#: First delay before asking a provider again for a sync it deferred for
#: lack of a slot; the n-th retry waits n times this. Long enough for the
#: provider's concurrent syncs to us to finish (an initial stream takes
#: seconds), short enough that a restarted household catches up in a minute
#: or two rather than at the next 30-minute tick.
DEFERRED_SYNC_RETRY_S: float = 20.0

#: Retries of one deferred (space, provider) sync before the periodic tick
#: is left to pick it up. Reset when that sync completes. A capacity
#: refusal spends none of the provider's 5 / h budget.
MAX_DEFERRED_SYNC_RETRIES: int = 6

#: How long a BEGIN we sent holds one of the provider's
#: :data:`MAX_ACTIVE_SESSIONS_PER_INSTANCE` slots in our own count when its
#: end never reaches us (an ICE failure, a lost ``__complete__``). Past it
#: the slot is taken back: a host that is in fact still busy answers
#: ``too_many_sessions`` and the deferral asks again — the cap can delay a
#: sync, never wedge one. A periodic stream normally ends in seconds.
INFLIGHT_BEGIN_TTL_S: float = 120.0

#: Which request wins when one space is asked for twice while it waits for
#: a slot — the one that streams more.
_MODE_RANK: dict[str, int] = {"incremental": 0, "initial": 1, "full": 2}

#: Delays between startup sweep passes, in seconds. A household that has just
#: rebooted cannot route anywhere yet: the confirmed-peer transports are still
#: coming up, so ``discover_route`` returns None and the BEGIN fails with
#: ``no_route``. That is transient and must be retried in seconds — waiting
#: for the next 30-minute tick is what left a restarted mesh member empty even
#: with the trigger in place. Bounded and finite: after the last pass the
#: periodic tick is the backstop.
STARTUP_MESH_CATCHUP_RETRY_DELAYS_S: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0)


class SpaceSyncScheduler:
    """Orchestrates sync initiation across pairs + spaces."""

    __slots__ = (
        "_applied_seqs",
        "_authority_echo",
        "_bus",
        "_deferred_attempts",
        "_deferred_tasks",
        "_drain_timers",
        "_federation",
        "_federation_repo",
        "_inflight",
        "_interval",
        "_mesh_announce",
        "_mesh_announced",
        "_mesh_catchup_attempts",
        "_mesh_catchup_done",
        "_own_instance_id",
        "_parked",
        "_queue",
        "_roster_refresh",
        "_seat_repo",
        "_space_repo",
        "_startup_task",
        "_stop",
        "_sync_manager",
        "_task",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation: FederationService,
        federation_repo: AbstractFederationRepo,
        space_repo: AbstractSpaceRepo,
        queue: ReconnectSyncQueue,
        sync_manager: SyncSessionManager,
        own_instance_id: str,
        interval_seconds: float = PERIODIC_INTERVAL_SECONDS,
        applied_seqs: AbstractSpaceSyncWatermarkRepo | None = None,
        seat_repo: AbstractSpaceRemoteMemberRepo | None = None,
    ) -> None:
        self._bus = bus
        #: The roster mirror: a trusted inviting admin household is a mesh
        #: catch-up target too (migration 0093). ``None``: owner only.
        self._seat_repo = seat_repo
        #: §25.6 echo (migration 0087): the provider snapshot of the last
        #: stream we applied cleanly, per (space, provider) — a periodic
        #: BEGIN carries it as ``have_seq``. ``None``: never sent, so every
        #: periodic session streams in full.
        self._applied_seqs = applied_seqs
        self._federation = federation
        self._federation_repo = federation_repo
        self._space_repo = space_repo
        self._queue = queue
        self._sync_manager = sync_manager
        self._own_instance_id = own_instance_id
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._startup_task: asyncio.Task | None = None
        #: ``(space_id, host_instance_id)`` seen to complete this process.
        #: The watermark is the protocol's own end-of-stream sentinel, not a
        #: content heuristic — ``stream_initial`` emits it unconditionally,
        #: even for a space with zero rows, so a legitimately empty space
        #: completes on the first attempt and never retries.
        self._mesh_catchup_done: set[tuple[str, str]] = set()
        self._mesh_catchup_attempts: dict[tuple[str, str], int] = {}
        #: v_32 roster self-heal, run once per tick (see
        #: :meth:`attach_roster_refresh`).
        self._roster_refresh: Callable[[], Awaitable[object]] | None = None
        #: v_46 authority epoch echo builder (see :meth:`attach_authority_echo`).
        self._authority_echo: Callable[[str, str], Awaitable[dict | None]] | None = None
        #: ``(space_id, provider)`` → deferred retries since it last completed.
        self._deferred_attempts: dict[tuple[str, str], int] = {}
        #: Pending deferred-retry waits (they end on ``_stop``).
        self._deferred_tasks: set[asyncio.Task] = set()
        #: Mesh-only host version announcer (see :meth:`attach_mesh_announce`).
        self._mesh_announce: Callable[[str], Awaitable[bool]] | None = None
        #: Mesh-only hosts our version reached this process.
        self._mesh_announced: set[str] = set()
        #: provider → {sync_id: (space_id, monotonic sent-at)} — the BEGINs
        #: we sent it that have not ended yet. A provider serves at most
        #: :data:`MAX_ACTIVE_SESSIONS_PER_INSTANCE` of ours at once and
        #: refuses the rest, so we never ask for more (trigger 8).
        self._inflight: dict[str, dict[str, tuple[str, float]]] = {}
        #: provider → {space_id: (sync_mode, priority)} waiting for a slot,
        #: one entry per space.
        self._parked: dict[str, dict[str, tuple[str, int]]] = {}
        #: provider → the timer that reclaims stale slots for its waiters.
        self._drain_timers: dict[str, asyncio.Task] = {}

    def attach_roster_refresh(self, refresh: Callable[[], Awaitable[object]]) -> None:
        """Run ``refresh`` on every periodic tick — the host re-sending each
        member household the roster of every space it hosts
        (``SpaceService.send_hosted_roster_snapshots``). Content authors are
        bound to that roster, so a mirror that missed gossip has to heal on
        its own, not wait for an operator."""
        self._roster_refresh = refresh

    def attach_authority_echo(
        self, echo: Callable[[str, str], Awaitable[dict | None]]
    ) -> None:
        """``echo(space_id, to_instance_id)`` → the ``authority_epoch_echo``
        a BEGIN to that household carries, or ``None``
        (``SpaceAuthorityRotationService.authority_epoch_echo``, v_46)."""
        self._authority_echo = echo

    def attach_mesh_announce(self, announce: Callable[[str], Awaitable[bool]]) -> None:
        """``announce(host_instance_id)`` → ``True`` once nothing more is
        needed (``CapabilitiesOutbound.announce_to_mesh_host``). Run by the
        mesh sweep for every host we reach only over the mesh: such a host
        holds no ``remote_instances`` row for us, so without it it could
        gate nothing on our version — no writer cert, writer key or channel
        grant for us (migration 0078)."""
        self._mesh_announce = announce

    def wire(self) -> None:
        """Subscribe to the bus events we act on. Idempotent."""
        self._bus.subscribe(PairingConfirmed, self._on_pairing_confirmed)
        self._bus.subscribe(SpaceSyncComplete, self._on_space_sync_complete)
        self._bus.subscribe(SpaceAuthorityEchoDue, self._on_authority_echo_due)
        self._bus.subscribe(SpaceSyncDeferred, self._on_sync_deferred)

    async def begin_fields(
        self, *, space_id: str, peer_instance_id: str, sync_mode: str
    ) -> dict[str, int]:
        """The §25.6 incremental fields of a BEGIN to ``peer_instance_id``.

        A periodic (``"incremental"``) session echoes ``have_seq`` — the
        provider's snapshot of the last stream from it we applied cleanly
        (migration 0087). It lives in our database, so after a restore from
        an older file snapshot it is the older value, and the provider
        re-streams what we lost instead of trusting its own watermark.
        Nothing recorded (or the read failed): no field — the provider then
        streams in full. Other modes stream in full anyway.
        """
        if sync_mode != "incremental" or self._applied_seqs is None:
            return {}
        try:
            seq = await self._applied_seqs.applied_seq(space_id, peer_instance_id)
        except Exception:
            log.exception(
                "space sync: reading the applied snapshot of %s for %s failed",
                peer_instance_id,
                space_id,
            )
            return {}
        return {} if seq is None else {"have_seq": int(seq)}

    async def _echo_for(self, space_id: str, to_instance_id: str) -> dict | None:
        if self._authority_echo is None:
            return None
        try:
            return await self._authority_echo(space_id, to_instance_id)
        except Exception:
            log.exception("authority epoch echo for %s failed", space_id)
            return None

    async def _on_authority_echo_due(self, event: SpaceAuthorityEchoDue) -> None:
        """Tell the owner now, not on the next periodic tick (v_46)."""
        owner = event.owner_instance_id
        if not owner or owner == self._own_instance_id:
            return
        if await self._federation.is_confirmed_peer(owner):
            # Security priority: ahead of any bulk sync queued in front of it.
            # The owner spends its per-household re-send slot only on an
            # actual re-send, and a proven forgotten epoch never needs one,
            # so a periodic echo just before this one cannot swallow it.
            await self.enqueue_sync_for_space(
                space_id=event.space_id, peer_instance_id=owner, priority=P1_SECURITY
            )
            return
        await self._federation.begin_mesh_catchup_sync(
            space_id=event.space_id,
            host_instance_id=owner,
            extra_payload=await self._echo_for(event.space_id, owner),
        )

    async def _on_space_sync_complete(self, event: SpaceSyncComplete) -> None:
        """Record the end-of-stream sentinel as the catch-up watermark.

        Published by the receiver when the ``__complete__`` chunk lands
        (``sync/space/receiver.py``). Until this fires for a (space, host)
        the mesh catch-up trigger keeps re-issuing BEGIN, which is what
        recovers a requester that restarted mid-stream.
        """
        self._mesh_catchup_done.add((event.space_id, event.from_instance))
        self._deferred_attempts.pop((event.space_id, event.from_instance), None)
        # Its slot at the provider is free: the next waiting space goes.
        self._release_slot(event.from_instance, event.sync_id, event.space_id)
        await self._drain_parked(event.from_instance)

    async def _on_sync_deferred(self, event: SpaceSyncDeferred) -> None:
        """A provider had no free slot for our BEGIN: ask again shortly —
        the sync was never served (trigger 5). The retry asks for the same
        mode the refused BEGIN did."""
        # The refused BEGIN holds no session there, so its slot is free for
        # its own retry below. Nothing waiting is pulled in on a refusal:
        # the host has just said it is full.
        self._release_slot(event.provider_instance_id, event.sync_id, event.space_id)
        key = (event.space_id, event.provider_instance_id)
        attempts = self._deferred_attempts.get(key, 0)
        if attempts >= MAX_DEFERRED_SYNC_RETRIES:
            log.warning(
                "space sync %s from %s deferred %d times — left to the periodic tick",
                event.space_id,
                event.provider_instance_id,
                attempts,
            )
            return
        self._deferred_attempts[key] = attempts + 1
        task = asyncio.create_task(
            self._retry_deferred(
                event.space_id,
                event.provider_instance_id,
                DEFERRED_SYNC_RETRY_S * (attempts + 1),
                event.sync_mode,
            ),
            name=f"SpaceSyncScheduler-deferred-{event.space_id}",
        )
        self._deferred_tasks.add(task)
        task.add_done_callback(self._deferred_tasks.discard)

    async def _retry_deferred(
        self, space_id: str, provider: str, delay: float, sync_mode: str
    ) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
            return  # stopped while waiting
        except TimeoutError:
            pass
        await self.enqueue_sync_for_space(
            space_id=space_id,
            peer_instance_id=provider,
            priority=P6_PRODUCTIVITY,
            sync_mode=sync_mode,
        )

    # ─── Per-provider slots (trigger 8) ────────────────────────────

    def _free_slots(self, provider: str) -> int:
        """Slots of ``provider``'s per-household cap we may still use;
        reclaims BEGINs older than :data:`INFLIGHT_BEGIN_TTL_S` first."""
        held = self._inflight.get(provider)
        if held:
            cutoff = time.monotonic() - INFLIGHT_BEGIN_TTL_S
            for sync_id in [s for s, (_, at) in held.items() if at < cutoff]:
                del held[sync_id]
            if not held:
                del self._inflight[provider]
        return MAX_ACTIVE_SESSIONS_PER_INSTANCE - len(self._inflight.get(provider, {}))

    def _release_slot(self, provider: str, sync_id: str, space_id: str) -> None:
        """A BEGIN to ``provider`` ended. Addressed by ``sync_id``; an event
        without one frees the slot its space holds."""
        held = self._inflight.get(provider)
        if not held:
            return
        if sync_id and sync_id in held:
            del held[sync_id]
        elif not sync_id:
            match = next((s for s, (sp, _) in held.items() if sp == space_id), None)
            if match is not None:
                del held[match]
        if not held:
            del self._inflight[provider]

    def _park(
        self, provider: str, space_id: str, sync_mode: str, priority: int
    ) -> None:
        """Hold a sync for ``provider`` until a slot frees. One entry per
        space: the mode that streams more and the more urgent priority win."""
        waiting = self._parked.setdefault(provider, {})
        held = waiting.get(space_id)
        if held is not None:
            mode, prio = held
            if _MODE_RANK.get(mode, 1) > _MODE_RANK.get(sync_mode, 1):
                sync_mode = mode
            priority = min(prio, priority)
        waiting[space_id] = (sync_mode, priority)
        timer = self._drain_timers.get(provider)
        if timer is None or timer.done():
            self._drain_timers[provider] = asyncio.create_task(
                self._drain_after_ttl(provider),
                name=f"SpaceSyncScheduler-drain-{provider}",
            )

    async def _drain_after_ttl(self, provider: str) -> None:
        """Backstop for slots whose end never reaches us: once they age
        out, the waiting spaces go. Re-arms while anything still waits."""
        while self._parked.get(provider) and not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=INFLIGHT_BEGIN_TTL_S)
                return  # stopped while waiting
            except TimeoutError:
                pass
            await self._drain_parked(provider)

    async def _drain_parked(self, provider: str) -> None:
        """Ask for as many waiting spaces as ``provider`` has free slots,
        most urgent first. Each re-checks the cap when the queue runs it."""
        waiting = self._parked.get(provider)
        if not waiting:
            return
        free = self._free_slots(provider)
        if free <= 0:
            return
        order = sorted(waiting.items(), key=lambda item: item[1][1])[:free]
        for space_id, (sync_mode, priority) in order:
            del waiting[space_id]
            await self.enqueue_sync_for_space(
                space_id=space_id,
                peer_instance_id=provider,
                priority=priority,
                sync_mode=sync_mode,
            )
        if not waiting:
            del self._parked[provider]

    async def start(self) -> None:
        """Begin the periodic tick. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="SpaceSyncScheduler")
        if self._startup_task is None or self._startup_task.done():
            self._startup_task = asyncio.create_task(
                self._startup_mesh_catchup(),
                name="SpaceSyncScheduler-startup-mesh-catchup",
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._deferred_tasks:
            # Each wait ends on ``_stop``; nothing is mid-write.
            await asyncio.gather(*self._deferred_tasks, return_exceptions=True)
        if self._drain_timers:
            # Same: each timer waits on ``_stop`` and returns.
            await asyncio.gather(*self._drain_timers.values(), return_exceptions=True)
            self._drain_timers.clear()
        if self._startup_task is not None:
            # Waits on ``self._stop`` internally, so setting the event above
            # is enough for it to return on its own.
            try:
                await asyncio.wait_for(self._startup_task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._startup_task.cancel()
            self._startup_task = None
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def enqueue_sync_for_space(
        self,
        *,
        space_id: str,
        peer_instance_id: str,
        priority: int = P4_DM,
        sync_mode: str = "initial",
    ) -> None:
        """Queue a sync from us to ``peer_instance_id`` for ``space_id``.

        ``sync_mode`` ``"incremental"`` is the periodic re-sync of a space
        we already hold: the provider re-ships no media and streams only the
        rows changed since the last stream we confirmed (its per-household
        watermark, migration 0086 — a full stream when it has none). A
        pairing, an echo or "Sync now" is ``"initial"`` and also enqueues
        the catch-up media; a deferred retry asks for what was refused.
        While the provider has no free slot for us (trigger 8) the sync
        waits for one rather than being sent.

        Fire-and-forget — the actual ``SPACE_SYNC_BEGIN`` send happens
        when the queue worker picks up the task. Callers should not
        await completion.
        """

        async def _task() -> None:
            # The queue may run this well after it was enqueued: a household
            # unpaired in between (its row purged) has no direct path left —
            # ``send_event`` could only drop the BEGIN as an unknown
            # instance. A member still reachable over the mesh is the mesh
            # catch-up's to sync (``_tick_mesh_catchup``).
            if await self._federation_repo.get_instance(peer_instance_id) is None:
                log.debug(
                    "space sync %s → %s skipped: no longer a paired household",
                    space_id,
                    peer_instance_id,
                )
                return
            # Trigger 8: the provider serves at most
            # MAX_ACTIVE_SESSIONS_PER_INSTANCE syncs of ours at once and
            # refuses the rest — wait for a slot instead of asking anyway.
            if self._free_slots(peer_instance_id) <= 0:
                self._park(peer_instance_id, space_id, sync_mode, priority)
                return
            # Debug knob — when set, the scheduler always asks for
            # HTTPS-mode (Part C) so the federation-demo harness can
            # exercise the fallback path without staging a real ICE
            # failure. Production deployments leave this unset; the
            # runtime negotiates DataChannel first and falls back via
            # ``trigger_relay_sync`` on the 15 s ICE timeout.
            prefer_direct = os.environ.get("SH_FORCE_SYNC_HTTPS") != "1"
            # A household seated from an invite link is relay-only: no
            # address either way, and its direct-sync signalling is refused
            # by the peer-class step — ask it for relay mode straight away.
            if prefer_direct and await self._federation.is_space_session_peer(
                peer_instance_id
            ):
                prefer_direct = False
            sync_id = uuid.uuid4().hex
            self._inflight.setdefault(peer_instance_id, {})[sync_id] = (
                space_id,
                time.monotonic(),
            )
            fields = await self.begin_fields(
                space_id=space_id,
                peer_instance_id=peer_instance_id,
                sync_mode=sync_mode,
            )
            # Record what we are asking for, so the provider's
            # ``SPACE_SYNC_OFFER`` can be recognised as an ANSWER. An
            # offer for a sync_id nobody here issued is refused. The mode
            # and echo ride along so a relay retry after an ICE timeout
            # asks for the same stream.
            self._federation.record_sync_request(
                sync_id=sync_id,
                space_id=space_id,
                provider_instance_id=peer_instance_id,
                sync_mode=sync_mode,
                have_seq=fields.get("have_seq"),
            )
            payload: dict = {
                "sync_id": sync_id,
                "space_id": space_id,
                "sync_mode": sync_mode,
                "prefer_direct": prefer_direct,
                **fields,
            }
            echo = await self._echo_for(space_id, peer_instance_id)
            if echo is not None:
                payload["authority_epoch_echo"] = echo
            try:
                await self._federation.send_event(
                    to_instance_id=peer_instance_id,
                    event_type=FederationEventType.SPACE_SYNC_BEGIN,
                    payload=payload,
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
            except TimeoutError:
                continue

    async def _tick_once(self) -> None:
        # Backstop: reap any abandoned in-memory sync sessions. Wrapped so
        # a reaper error never kills the tick's sync-enqueue work below.
        try:
            self._sync_manager.reap_stale(STALE_SESSION_TTL_SECONDS)
        except Exception:
            log.exception("space-sync-scheduler: reap_stale failed")

        # Mesh-only hosts (#648) — wrapped so a failure here never costs
        # the confirmed-peer sweep below.
        try:
            # Return value (pairs still pending) matters only to the startup
            # sweep's retry loop; the periodic tick just runs again later.
            await self._tick_mesh_catchup()
        except Exception:
            log.exception("space-sync-scheduler: mesh catch-up failed")
        await self._tick_mesh_announce()

        if self._roster_refresh is not None:
            try:
                await self._roster_refresh()
            except Exception:
                log.exception("space-sync-scheduler: roster refresh failed")

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
                    sync_mode="incremental",
                )

    # ─── Mesh catch-up (#648) ───────────────────────────────────────

    async def _startup_mesh_catchup(self) -> None:
        """Run one mesh catch-up sweep shortly after boot.

        Triggers 1 and 2 both walk CONFIRMED peers, so neither ever fires
        for a mesh-only host. Without this a restarted household waits a
        full periodic interval before asking for the metadata it lost.
        """
        for delay in (
            STARTUP_MESH_CATCHUP_DELAY_SECONDS,
            *STARTUP_MESH_CATCHUP_RETRY_DELAYS_S,
        ):
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # stopped while waiting
            except TimeoutError:
                pass
            try:
                pending = await self._tick_mesh_catchup()
            except Exception:  # pragma: no cover
                log.exception("startup mesh catch-up sweep failed")
                return
            pending += await self._tick_mesh_announce()
            if not pending:
                # Every mesh-only host either got its BEGIN, is already
                # complete, or hit the attempt cap. Nothing left to retry —
                # the periodic tick covers anything that regresses later.
                return

    async def _tick_mesh_catchup(self) -> int:
        """Re-issue ``SPACE_SYNC_BEGIN`` for each incomplete mesh-only host.

        ``Space.owner_instance_id`` is the host, so the predicate is exact
        rather than a heuristic: a space we don't own whose owner is not a
        confirmed peer is reachable only over the mesh. Only spaces we hold
        a non-follower seat in (:meth:`_list_joined_remote_spaces`) — a
        space we only follow over a GFS never reveals us to its host. Deliberately calls
        ``begin_mesh_catchup_sync`` and NOT ``enqueue_sync_for_space`` —
        the latter ships a bare ``send_event`` with no requester-side
        receive session, which cannot serve a mesh host at all.

        Returns the number of (space, host) pairs that are still **not
        complete** and still under the attempt cap.

        "Complete" is the sentinel watermark, never "we managed to send a
        BEGIN": a delivered BEGIN only means the host started streaming. It
        can still abandon the stream — e.g. the host invalidates its cached
        route to a just-rebooted requester (as it must, or it would seal
        under a dead key) and then cannot re-discover one, because the
        requester→host direction comes up before host→requester. Treating a
        shipped BEGIN as done left exactly that case waiting for the next
        30-minute tick (#648).
        """
        pending = 0
        pairs: list[tuple[Space, str]] = []
        for joined in await self._list_joined_remote_spaces():
            for target in await self._mesh_targets(joined):
                pairs.append((joined, target))
        for space, host in pairs:
            key = (space.id, host)
            if key in self._mesh_catchup_done:
                # Sentinel observed — this pair is genuinely caught up.
                continue
            if await self._federation.is_confirmed_peer(host):
                # Triggers 1/2 own this peer; nothing to do here.
                continue
            attempts = self._mesh_catchup_attempts.get(key, 0)
            if attempts >= MAX_MESH_CATCHUP_ATTEMPTS:
                continue
            # Not complete and not capped → still pending, whatever this
            # attempt does. Counted before the send so a shipped-but-doomed
            # stream keeps the retry loop alive.
            pending += 1
            try:
                shipped = await self._federation.begin_mesh_catchup_sync(
                    space_id=space.id,
                    host_instance_id=host,
                    # The authority echo is the owner's business alone.
                    extra_payload=(
                        await self._echo_for(space.id, host)
                        if host == space.owner_instance_id
                        else None
                    ),
                )
            except Exception:  # pragma: no cover — begin_* is fail-soft
                log.exception(
                    "mesh catch-up BEGIN failed: space=%s host=%s",
                    space.id,
                    host,
                )
                shipped = False
            if shipped:
                # Only a BEGIN the host actually received counts against the
                # cap — a ``no_route`` never reached the host, so it never
                # touched the host's 5/h budget either.
                self._mesh_catchup_attempts[key] = attempts + 1
        return pending

    async def _tick_mesh_announce(self) -> int:
        """Announce our version to each mesh-only space host not yet reached
        this process. Returns how many are still pending. Never raises."""
        if self._mesh_announce is None:
            return 0
        pending = 0
        try:
            targets: set[str] = set()
            for joined in await self._list_joined_remote_spaces():
                targets.update(await self._mesh_targets(joined))
            hosts = sorted(targets)
        except Exception:  # pragma: no cover — defensive
            log.exception("space-sync-scheduler: listing mesh hosts failed")
            return 0
        for host in hosts:
            if host in self._mesh_announced:
                continue
            try:
                if await self._federation.is_confirmed_peer(host):
                    continue
                done = await self._mesh_announce(host)
            except Exception:
                log.exception("mesh version announcement to %s failed", host)
                done = False
            if done:
                self._mesh_announced.add(host)
            else:
                pending += 1
        return pending

    async def _mesh_targets(self, space: Space) -> list[str]:
        """Who a mesh catch-up / version announcement for *space* goes to:
        its owner, and — on a household a delegated admin invited — that
        inviting admin household while it is host-equivalent here
        (:mod:`socialhome.federation.host_equivalence`). The owner may not
        know of us yet, or be offline; the inviter seated us."""
        targets = [space.owner_instance_id]
        if self._seat_repo is not None:
            inviter = await self._space_repo.get_invited_by(space.id)
            if (
                inviter
                and inviter not in targets
                and inviter != self._own_instance_id
                and await acts_as_host(
                    self._space_repo, self._seat_repo, space, inviter
                )
            ):
                targets.append(inviter)
        return targets

    async def _list_joined_remote_spaces(self) -> list[Space]:
        """Spaces hosted elsewhere in which this household holds a local
        seat other than a follower (``subscriber``) one.

        The mesh sweeps (catch-up BEGIN, version announcement) contact the
        host directly, which tells it we exist and care about the space. A
        PUBLIC space we only follow — or merely mirror from a GFS listing,
        with no seat at all — must stay invisible to its host: the GFS
        shields followers from hosts. Only real membership justifies it.
        """
        out: list[Space] = []
        for space in await self._list_local_spaces():
            host = space.owner_instance_id
            if not host or host == self._own_instance_id:
                continue
            members = await self._space_repo.list_members(space.id)
            if any(str(m.role) != SpaceRole.SUBSCRIBER.value for m in members):
                out.append(space)
        return out

    async def _list_local_spaces(self) -> list[Space]:
        spaces = (
            await self._space_repo.list_by_type(SpaceType.HOUSEHOLD)
            + await self._space_repo.list_by_type(SpaceType.PUBLIC)
            + await self._space_repo.list_by_type(SpaceType.PRIVATE)
        )
        return [s for s in spaces if not s.dissolved]

    async def _list_local_space_ids(self) -> list[str]:
        return [s.id for s in await self._list_local_spaces()]

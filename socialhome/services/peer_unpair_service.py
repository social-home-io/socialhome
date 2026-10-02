"""Tear down a household pairing — both directions, one code path (§11).

A pairing ends in one of two ways:

* **Locally** — our admin removes the connection
  (``DELETE /api/pairing/connections/{instance_id}``). :meth:`unpair`
  first tells the peer with a signed ``UNPAIR`` envelope, *then* forgets
  it. The order matters: the envelope is encrypted under the pairwise
  session key and delivered to the inbox URL stored on the
  ``remote_instances`` row, so once that row is gone we can no longer
  reach the peer at all.
* **Remotely** — the peer's ``UNPAIR`` arrives through the §24.11
  pipeline (signature verified against the key we pinned at pairing),
  and :class:`~socialhome.services.federation_inbound.PairingInboundHandlers`
  calls :meth:`forget`.

Both paths share :meth:`forget`, so the local state a pairing leaves
behind is cleaned up identically whichever side ended it: queued outbox
envelopes for the peer, the mesh hints it announced to us
(``network_discovery`` rows it is the source of), the
``remote_instances`` row itself (``remote_users`` /
``peer_user_visibility`` cascade), and a :class:`PeerUnpaired` event for
the realtime bridge.

Deliberately **not** touched: space membership (``space_instances`` /
remote members). A space is shared by its members, not by the pairing —
two households can stay co-members of a space and keep receiving its
content over the mesh after they stop being direct connections.

**A peer that is offline at unpair time.** The first notify is bounded by
:data:`UNPAIR_NOTIFY_TIMEOUT_S` so an unreachable peer never blocks the
admin. If it does not land, the pairing becomes an *unpair tombstone*
instead of being deleted: the row stays in
:data:`~socialhome.domain.federation.PairingStatus.UNPAIRING` because the
outbox needs its session key and inbox URL to keep retrying the
``UNPAIR``. The tombstone grants no trust — every ordinary read hides it
(:meth:`AbstractFederationRepo.get_instance`), the §24.11 pipeline refuses
everything it sends except its own ``UNPAIR``, and redelivery sends it
nothing but the ``UNPAIR`` — and it disappears from the UI at once
(:class:`PeerUnpaired`). It ends when:

* the ``UNPAIR`` is delivered, or refused for good (the peer already
  forgot us) — :meth:`finish_unpair`, called from the outbox's delivery
  callback;
* the peer's own ``UNPAIR`` reaches us — :meth:`forget`;
* the queued ``UNPAIR`` outlives :data:`UNPAIR_RETRY_MAX_AGE` — the
  outbox retention sweep fails it and :meth:`sweep_tombstones` purges the
  row;
* we pair with that household again — the new row replaces it
  (``SqliteFederationRepo.save_instance``).

The retry state lives entirely in the one queued outbox row (its backoff,
its ``expires_at``), so the tombstone needs no column of its own.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

from ..domain.events import PeerUnpaired
from ..domain.federation import FederationEventType, PairingStatus
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.dm_media_outbox_repo import AbstractDmMediaOutboxRepo
    from ..repositories.dm_routing_repo import AbstractDmRoutingRepo
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.outbox_repo import AbstractOutboxRepo
    from ..repositories.space_media_outbox_repo import AbstractSpaceMediaOutboxRepo

log = logging.getLogger(__name__)

#: Upper bound on how long a local unpair waits for the ``UNPAIR``
#: envelope to reach the peer before leaving the retry to the outbox.
UNPAIR_NOTIFY_TIMEOUT_S: float = 5.0

#: How long the outbox keeps retrying the ``UNPAIR`` to a peer that was
#: offline at unpair time. Past this the tombstone is purged and the peer,
#: if it ever returns, is refused as a household we never knew.
UNPAIR_RETRY_MAX_AGE: timedelta = timedelta(days=30)


async def is_unpair_tombstone(
    federation_repo: "AbstractFederationRepo | None",
    instance_id: str,
) -> bool:
    """Whether *instance_id* is an unpair tombstone.

    For the side outboxes (DM / space media) whose senders do not go
    through the federation outbox's tombstone gate: a tombstone gets our
    ``UNPAIR`` and nothing else. ``None`` repo (not wired) → ``False``.
    """
    if federation_repo is None:
        return False
    inst = await federation_repo.get_instance(instance_id, include_unpairing=True)
    return inst is not None and inst.status is PairingStatus.UNPAIRING


class InstancePurger(Protocol):
    """Drops a household's ``remote_instances`` row and everything queued or
    learned because of it. Every deletion of a ``remote_instances`` row goes
    through :meth:`PeerUnpairService.purge` (unpair, space-session cleanup,
    ``PAIRING_ABORT``), so no path can leave its outbox behind."""

    async def purge(self, instance_id: str) -> None: ...


class PeerUnpairService:
    """Local unpair (notify, then forget or tombstone) and the shared
    forget step."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_outbox_repo",
        "_routing_repo",
        "_dm_media_outbox_repo",
        "_space_media_outbox_repo",
        "_notify_timeout_s",
        "_in_progress",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation: "FederationService",
        federation_repo: "AbstractFederationRepo",
        outbox_repo: "AbstractOutboxRepo",
        routing_repo: "AbstractDmRoutingRepo",
        dm_media_outbox_repo: "AbstractDmMediaOutboxRepo",
        space_media_outbox_repo: "AbstractSpaceMediaOutboxRepo",
        notify_timeout_s: float = UNPAIR_NOTIFY_TIMEOUT_S,
    ) -> None:
        self._bus = bus
        self._federation = federation
        self._federation_repo = federation_repo
        self._outbox_repo = outbox_repo
        self._routing_repo = routing_repo
        self._dm_media_outbox_repo = dm_media_outbox_repo
        self._space_media_outbox_repo = space_media_outbox_repo
        self._notify_timeout_s = notify_timeout_s
        #: Tombstones :meth:`_tombstone` is still building (status flipped,
        #: UNPAIR not queued yet) — :meth:`sweep_tombstones` must not take
        #: one for an abandoned tombstone. In memory is enough: the sweep
        #: and the unpair run in this one process, and a crash mid-build
        #: leaves exactly the orphan the sweep exists to collect.
        self._in_progress: set[str] = set()

    async def unpair(self, instance_id: str) -> bool | None:
        """Tell the peer, then forget it.

        Returns ``None`` when there is no pairing with ``instance_id``,
        else whether the peer's transport accepted the ``UNPAIR``
        envelope right away. ``False`` = it did not; the pairing is gone
        locally either way, and the ``UNPAIR`` stays queued (see the
        module docstring) until it lands or expires.
        """
        if await self._federation_repo.get_instance(instance_id) is None:
            return None
        if await self._notify_peer(instance_id):
            await self.forget(instance_id)
            return True
        await self._tombstone(instance_id)
        return False

    async def forget(self, instance_id: str) -> None:
        """Drop every piece of local state that exists only because we were
        paired with ``instance_id``, then publish :class:`PeerUnpaired`.

        Shared by the local unpair and the inbound ``UNPAIR`` handler.
        """
        await self.purge(instance_id)
        await self._bus.publish(PeerUnpaired(instance_id=instance_id))

    async def finish_unpair(self, instance_id: str) -> None:
        """The queued ``UNPAIR`` to *instance_id* reached a verdict —
        delivered, or refused for good. Purge the tombstone.

        A no-op unless the row is still a tombstone: an ``UNPAIR`` outcome
        that arrives after we paired with that household again must never
        tear the new pairing down. Publishes nothing — the connection
        already left the UI when the tombstone was made.
        """
        inst = await self._federation_repo.get_instance(
            instance_id,
            include_unpairing=True,
        )
        if inst is None or inst.status is not PairingStatus.UNPAIRING:
            return
        log.info("unpair: UNPAIR to %s settled — tombstone purged", instance_id)
        await self.purge(instance_id)

    async def sweep_tombstones(self) -> int:
        """Purge every tombstone whose ``UNPAIR`` is no longer queued.

        Runs after the outbox retention sweep, which fails a queued
        ``UNPAIR`` once it is :data:`UNPAIR_RETRY_MAX_AGE` old — so this is
        where the max age is enforced. It also collects a tombstone that
        lost its ``UNPAIR`` any other way (a crash between the two writes,
        an operator clearing the outbox). Returns how many were purged.
        """
        purged = 0
        for inst in await self._federation_repo.list_instances(
            status=PairingStatus.UNPAIRING.value,
        ):
            if inst.id in self._in_progress:
                continue
            if await self._outbox_repo.count_pending_for(inst.id):
                continue
            log.warning(
                "unpair: gave up telling %s it was unpaired — tombstone purged",
                inst.id,
            )
            await self.purge(inst.id)
            purged += 1
        return purged

    async def _tombstone(self, instance_id: str) -> None:
        self._in_progress.add(instance_id)
        try:
            await self._build_tombstone(instance_id)
        finally:
            self._in_progress.discard(instance_id)

    async def _build_tombstone(self, instance_id: str) -> None:
        # Trust closes first: from the status flip on, no read treats the
        # household as a peer and the pipeline refuses what it sends.
        # (Queueing the UNPAIR before the flip instead would race the outbox
        # processor: redelivery drops an UNPAIR to a live pairing as
        # superseded. The sweep race is closed by ``_in_progress``.)
        await self._federation_repo.mark_unpairing(instance_id)
        # Nothing but the UNPAIR may go out any more (send_event may just
        # have queued a copy of it, too — it is replaced by the one below).
        # Media bytes ride their own outboxes; the pipeline would refuse
        # them too, so they go now as well.
        await self._outbox_repo.delete_for_instance(instance_id)
        await self._dm_media_outbox_repo.delete_for_instance(instance_id)
        await self._space_media_outbox_repo.delete_for_instance(instance_id)
        await self._routing_repo.forget_discovered_via(instance_id)
        expires_at = (datetime.now(timezone.utc) + UNPAIR_RETRY_MAX_AGE).isoformat()
        queued = await self._federation.queue_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.UNPAIR,
            payload={},
            expires_at=expires_at,
        )
        if queued is None:
            # Cannot seal for this peer at all (session key unreadable):
            # a tombstone would only linger. Forget it now.
            log.warning(
                "unpair: cannot queue UNPAIR for %s — removing the pairing "
                "locally; the peer will not be told",
                instance_id,
            )
            await self.forget(instance_id)
            return
        log.info(
            "unpair: %s unreachable — UNPAIR queued for retry until %s",
            instance_id,
            expires_at,
        )
        await self._bus.publish(PeerUnpaired(instance_id=instance_id))

    async def purge(self, instance_id: str) -> None:
        """Delete ``instance_id``'s ``remote_instances`` row, then everything
        queued for it and every mesh hint it announced. Publishes nothing.

        The one way a service removes a ``remote_instances`` row (see
        :class:`InstancePurger`). The repository itself drops rows only in
        bulk housekeeping (expired pending handshakes, a tombstone replaced
        by a re-pair), and runs these same deletes in this same order inside
        that transaction. The row goes FIRST: from then on the
        outboxes refuse to queue for this household (their INSERTs check
        ``remote_instances``), so the deletes below sweep everything a send
        still in flight could have queued. The reverse order left a window
        where such a send saw the row, queued, and stranded an envelope
        behind the purge. A crash between the steps leaves rows addressed to
        nobody, which the outbox sweep
        (:meth:`~socialhome.repositories.outbox_repo.SqliteOutboxRepo.purge_orphaned`)
        drops.
        """
        await self._federation_repo.delete_instance(instance_id)
        await self._outbox_repo.delete_for_instance(instance_id)
        await self._dm_media_outbox_repo.delete_for_instance(instance_id)
        await self._space_media_outbox_repo.delete_for_instance(instance_id)
        await self._routing_repo.forget_discovered_via(instance_id)

    async def _notify_peer(self, instance_id: str) -> bool:
        # The payload is empty: the receiver unpairs the *signer*
        # (``from_instance``, bound to the verified signature), never an
        # id named in the body — so there is nothing to carry.
        try:
            result = await asyncio.wait_for(
                self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=FederationEventType.UNPAIR,
                    payload={},
                ),
                timeout=self._notify_timeout_s,
            )
        except TimeoutError:
            log.warning(
                "unpair: UNPAIR to %s timed out after %gs — queued for retry",
                instance_id,
                self._notify_timeout_s,
            )
            return False
        except Exception:
            # send_event reports delivery failures in its result; anything
            # raised here is a bug below us — still never strand the unpair.
            log.warning(
                "unpair: UNPAIR to %s failed — queued for retry",
                instance_id,
                exc_info=True,
            )
            return False
        if not result.ok:
            log.warning(
                "unpair: UNPAIR to %s not delivered (%s) — queued for retry",
                instance_id,
                result.error or result.status_code,
            )
            return False
        return True

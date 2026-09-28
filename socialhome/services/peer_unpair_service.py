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

The notify step is best-effort and bounded by
:data:`UNPAIR_NOTIFY_TIMEOUT_S`: an unreachable peer must never block the
admin's unpair. It cannot fall back to the outbox, because redelivery
needs the very ``remote_instances`` row (session key, inbox URL, relay)
that the unpair deletes — the outbox processor drops a row whose
instance is gone as PERMANENT.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..domain.events import PeerUnpaired
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.dm_routing_repo import AbstractDmRoutingRepo
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.outbox_repo import AbstractOutboxRepo

log = logging.getLogger(__name__)

#: Upper bound on how long a local unpair waits for the ``UNPAIR``
#: envelope to reach the peer before forgetting it anyway.
UNPAIR_NOTIFY_TIMEOUT_S: float = 5.0


class PeerUnpairService:
    """Local unpair (notify, then forget) and the shared forget step."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_outbox_repo",
        "_routing_repo",
        "_notify_timeout_s",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation: "FederationService",
        federation_repo: "AbstractFederationRepo",
        outbox_repo: "AbstractOutboxRepo",
        routing_repo: "AbstractDmRoutingRepo",
        notify_timeout_s: float = UNPAIR_NOTIFY_TIMEOUT_S,
    ) -> None:
        self._bus = bus
        self._federation = federation
        self._federation_repo = federation_repo
        self._outbox_repo = outbox_repo
        self._routing_repo = routing_repo
        self._notify_timeout_s = notify_timeout_s

    async def unpair(self, instance_id: str) -> bool | None:
        """Tell the peer, then forget it.

        Returns ``None`` when there is no pairing with ``instance_id``,
        else whether the peer's transport accepted the ``UNPAIR``
        envelope (``False`` = timed out / unreachable; the pairing is
        removed locally either way).
        """
        if await self._federation_repo.get_instance(instance_id) is None:
            return None
        notified = await self._notify_peer(instance_id)
        await self.forget(instance_id)
        return notified

    async def forget(self, instance_id: str) -> None:
        """Drop every piece of local state that exists only because we were
        paired with ``instance_id``, then publish :class:`PeerUnpaired`.

        Shared by the local unpair and the inbound ``UNPAIR`` handler.
        """
        await self._outbox_repo.delete_for_instance(instance_id)
        await self._routing_repo.forget_discovered_via(instance_id)
        await self._federation_repo.delete_instance(instance_id)
        await self._bus.publish(PeerUnpaired(instance_id=instance_id))

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
                "unpair: UNPAIR to %s timed out after %gs — removing the "
                "pairing locally; the peer was not told",
                instance_id,
                self._notify_timeout_s,
            )
            return False
        except Exception:
            # send_event reports delivery failures in its result; anything
            # raised here is a bug below us — still never strand the unpair.
            log.warning(
                "unpair: UNPAIR to %s failed — removing the pairing locally",
                instance_id,
                exc_info=True,
            )
            return False
        if not result.ok:
            log.warning(
                "unpair: UNPAIR to %s not delivered (%s) — removing the "
                "pairing locally; the peer was not told",
                instance_id,
                result.error or result.status_code,
            )
            return False
        return True

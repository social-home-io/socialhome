"""Cancel a pairing that was never confirmed (§11).

A pending ``remote_instances`` row is a handshake in flight, not a
pairing, so it is not torn down the way :class:`PeerUnpairService` ends a
confirmed one (a signed ``UNPAIR``, then a tombstone that keeps retrying
it for weeks). The pending shapes and what each side holds:

* **QR half-pair** (``pending_received``) — on the household that showed
  the code once the scanner's signed peer-accept arrived, and on the
  scanner from the moment it scanned. Both sides hold a
  ``pending_pairings`` session (token, SAS) plus a keyed row; the other
  side has the same.
* **Trust-relay request** (``pending_sent``) — we asked a confirmed peer to
  introduce us to a third household. We hold a provisional row without a
  key and an in-memory request in
  :class:`~socialhome.federation.auto_pair_coordinator.AutoPairCoordinator`;
  the target holds the vouched request in its approval inbox, but no row
  and no channel to us.

(A code that nobody has scanned yet has no row at all — only a session,
which expires by itself after five minutes.)

:meth:`PairingCancelService.cancel_pending` goes in this order:

1. **Close every way to finish the handshake.** The ``pending_pairings``
   sessions behind the row are deleted — our own SAS entry, the peer's
   signed peer-accept / peer-confirm (direct or through a GFS) all start
   from the session and are refused without it — and the trust-relay
   request is forgotten, so a target that approves later is ignored.
2. **Tell the peer, best effort** — only when the row holds a key, i.e. the
   peer has state about us and a signed, encrypted channel exists: one
   ``PAIRING_ABORT`` (the existing handshake-cancel event; its receiver
   drops only the *signer's* pending session and half-pair, never a
   confirmed pairing). Bounded by :data:`PAIRING_CANCEL_NOTIFY_TIMEOUT_S`;
   a peer that does not answer is not retried (its half expires with its
   own session) and nothing it could not already see is sent.
3. **Forget the row** — atomically and only while it is still pending,
   with everything queued or learned for it
   (:meth:`~socialhome.repositories.federation_repo.AbstractFederationRepo.delete_pending_instance`).
   A pairing confirmed in the meantime is never torn down by a cancel:
   :class:`PairingAlreadyConfirmedError` (409). The SPA learns through
   :class:`PeerUnpaired` (``connection.removed``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

from ..domain.events import PeerUnpaired
from ..domain.federation import (
    FederationEventType,
    PairingAlreadyConfirmedError,
    PairingStatus,
    PendingPairingNotFoundError,
    RemoteInstance,
)
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)

#: Upper bound on how long a cancel waits for the ``PAIRING_ABORT`` to
#: reach the peer. The cancel completes either way.
PAIRING_CANCEL_NOTIFY_TIMEOUT_S: float = 5.0

#: The ``reason`` our ``PAIRING_ABORT`` carries.
PAIRING_CANCEL_REASON: str = "cancelled"

_PENDING: frozenset[PairingStatus] = frozenset(
    {PairingStatus.PENDING_SENT, PairingStatus.PENDING_RECEIVED}
)


class PendingAutoPairRequests(Protocol):
    """The originator side of the trust-relay flow
    (:class:`~socialhome.federation.auto_pair_coordinator.AutoPairCoordinator`)."""

    def forget_requests_to(self, target_instance_id: str) -> int: ...


class PairingCancelService:
    """Cancels a pending pairing: close the handshake, tell the peer, forget."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_auto_pair",
        "_notify_timeout_s",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation: "FederationService",
        federation_repo: "AbstractFederationRepo",
        auto_pair: PendingAutoPairRequests | None = None,
        notify_timeout_s: float = PAIRING_CANCEL_NOTIFY_TIMEOUT_S,
    ) -> None:
        self._bus = bus
        self._federation = federation
        self._federation_repo = federation_repo
        self._auto_pair = auto_pair
        self._notify_timeout_s = notify_timeout_s

    async def cancel_pending(self, instance_id: str) -> bool:
        """Cancel our pending pairing with *instance_id*.

        Returns whether the peer's transport accepted our ``PAIRING_ABORT``
        (``False`` too when there was no channel to tell it on). Raises
        :class:`PendingPairingNotFoundError` when there is no pairing with
        that household (an unpair tombstone is not one), and
        :class:`PairingAlreadyConfirmedError` when it is — or became —
        confirmed. Callers check the caller is an admin.
        """
        inst = await self._federation_repo.get_instance(instance_id)
        if inst is None:
            raise PendingPairingNotFoundError()
        if inst.status not in _PENDING:
            raise PairingAlreadyConfirmedError()

        tokens = await self._federation_repo.pop_pairings_for_inbox(
            inst.local_inbox_id,
        )
        if self._auto_pair is not None:
            self._auto_pair.forget_requests_to(instance_id)

        notified = False
        if _has_signed_channel(inst):
            notified = await self._notify_peer(instance_id, tokens)

        if not await self._federation_repo.delete_pending_instance(instance_id):
            current = await self._federation_repo.get_instance(instance_id)
            if current is not None and current.status not in _PENDING:
                # Confirmed between our read and the delete (the peer's
                # signed confirm, or our own SAS entry, got there first).
                log.info(
                    "pairing cancel: %s was confirmed meanwhile — kept",
                    instance_id,
                )
                raise PairingAlreadyConfirmedError()
            # Already gone (the peer's own abort, the expiry sweep): the
            # pairing is cancelled either way.
        log.info(
            "pairing cancel: pending pairing with %s cancelled (peer told: %s)",
            instance_id,
            notified,
        )
        await self._bus.publish(PeerUnpaired(instance_id=instance_id))
        return notified

    async def _notify_peer(self, instance_id: str, tokens: list[str]) -> bool:
        # The peer already holds the token (it is the one from the code);
        # the receiver binds it to the signer before dropping anything.
        payload: dict = {"reason": PAIRING_CANCEL_REASON}
        if tokens:
            payload = {"token": tokens[0], **payload}
        try:
            result = await asyncio.wait_for(
                self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=FederationEventType.PAIRING_ABORT,
                    payload=payload,
                ),
                timeout=self._notify_timeout_s,
            )
        except TimeoutError:
            log.warning(
                "pairing cancel: PAIRING_ABORT to %s timed out after %gs",
                instance_id,
                self._notify_timeout_s,
            )
            return False
        except Exception:
            log.warning(
                "pairing cancel: PAIRING_ABORT to %s failed",
                instance_id,
                exc_info=True,
            )
            return False
        if not result.ok:
            # A copy may sit in the outbox; it goes with the row.
            log.warning(
                "pairing cancel: PAIRING_ABORT to %s not delivered (%s)",
                instance_id,
                result.error or result.status_code,
            )
            return False
        return True


def _has_signed_channel(inst: RemoteInstance) -> bool:
    """Whether the half-pair holds the peer's identity key and our session
    key — what sealing and signing an envelope to it needs. A trust-relay
    request we sent has neither (the target never answered us)."""
    return bool(inst.remote_identity_pk and inst.key_self_to_remote)

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

1. **Cancel, atomically** (:meth:`~socialhome.repositories.federation_repo.AbstractFederationRepo.cancel_pending_instance`):
   in ONE transaction the ``pending_pairings`` sessions behind the row
   (by its inbox id and by the peer identity they name) and the row
   itself go — only while the row is still pending — with everything
   queued or learned for it. Our own SAS entry, the peer's signed
   peer-accept / peer-confirm (direct or through a GFS) and the
   trust-relay ack all confirm through
   :meth:`~socialhome.repositories.federation_repo.AbstractFederationRepo.promote_pending_instance`,
   which needs the same session and pending row in its own transaction:
   whichever commits first wins, and the loser changes nothing. A
   pairing confirmed first is never torn down by a cancel:
   :class:`PairingAlreadyConfirmedError` (409), and nothing was sent.
   The trust-relay request is then forgotten in memory too.
2. **Tell the peer, best effort** — only after the cancel committed, and
   only when the removed row held a key (the peer has state about us and
   a signed, encrypted channel exists): one ``PAIRING_ABORT`` for a
   pairing-code half-pair, or one ``UNPAIR`` for a trust-relay approval
   (its requester may already be confirmed; see :func:`_cancel_message`),
   sealed with the captured row (:meth:`FederationService.send_event_once` — one
   attempt, never queued). Its receiver drops only the *signer's* pending
   session and half-pair, never a confirmed pairing. Bounded by
   :data:`PAIRING_CANCEL_NOTIFY_TIMEOUT_S`; a peer that is not reached
   keeps its half until its own session expires, and nothing it could not
   already see is sent.
3. **Announce** :class:`PeerUnpaired` (``connection.removed``) — once: a
   second cancel finds nothing and is a 404.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

from ..domain.events import PeerUnpaired
from ..domain.federation import (
    PENDING_KIND_APPROVAL,
    FederationEventType,
    PairingAlreadyConfirmedError,
    PairingStatus,
    PendingPairingNotFoundError,
    RemoteInstance,
    pending_kind,
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
        "_auto_pair",
        "_bus",
        "_federation",
        "_federation_repo",
        "_notify_timeout_s",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation: FederationService,
        federation_repo: AbstractFederationRepo,
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
        :class:`PendingPairingNotFoundError` when there is no pending
        pairing with that household — unknown, an unpair tombstone, or
        already cancelled (a second cancel publishes nothing) — and
        :class:`PairingAlreadyConfirmedError` when it is, or became,
        confirmed: then nothing was removed and nothing was sent. Callers
        check the caller is an admin.
        """
        inst = await self._federation_repo.get_instance(instance_id)
        if inst is None:
            raise PendingPairingNotFoundError()
        if inst.status not in _PENDING:
            raise PairingAlreadyConfirmedError()

        # One transaction: the sessions (the only right to finish the
        # handshake) and the row go together, and only while the row is
        # still pending. A confirm that commits first wins; ours then finds
        # nothing to cancel.
        tokens = await self._federation_repo.cancel_pending_instance(instance_id)
        if tokens is None:
            current = await self._federation_repo.get_instance(instance_id)
            if current is not None and current.status not in _PENDING:
                log.info(
                    "pairing cancel: %s was confirmed meanwhile — kept",
                    instance_id,
                )
                raise PairingAlreadyConfirmedError()
            raise PendingPairingNotFoundError()
        if self._auto_pair is not None:
            self._auto_pair.forget_requests_to(instance_id)

        # Only now, with the cancel committed, does anything leave this
        # household — sealed with the keys of the row we just removed.
        notified = False
        if _has_signed_channel(inst):
            notified = await self._notify_peer(inst, tokens)
        log.info(
            "pairing cancel: pending pairing with %s cancelled (peer told: %s)",
            instance_id,
            notified,
        )
        await self._bus.publish(PeerUnpaired(instance_id=instance_id))
        return notified

    async def _notify_peer(self, inst: RemoteInstance, tokens: list[str]) -> bool:
        event_type, payload = _cancel_message(inst, tokens)
        try:
            result = await asyncio.wait_for(
                self._federation.send_event_once(
                    instance=inst,
                    event_type=event_type,
                    payload=payload,
                ),
                timeout=self._notify_timeout_s,
            )
        except TimeoutError:
            log.warning(
                "pairing cancel: %s to %s timed out after %gs",
                event_type.value,
                inst.id,
                self._notify_timeout_s,
            )
            return False
        except Exception:
            log.warning(
                "pairing cancel: %s to %s failed",
                event_type.value,
                inst.id,
                exc_info=True,
            )
            return False
        if not result.ok:
            # One attempt, never queued: the row is gone, and the peer's
            # half expires with its own session.
            log.warning(
                "pairing cancel: %s to %s not delivered (%s)",
                event_type.value,
                inst.id,
                result.error or result.status_code,
            )
            return False
        return True


def _cancel_message(
    inst: RemoteInstance, tokens: list[str]
) -> tuple[FederationEventType, dict]:
    """What tells the peer: a ``PAIRING_ABORT`` for a pairing-code
    half-pair; an ``UNPAIR`` for a trust-relay approval.

    The requester of an approved introduction may already have confirmed
    on our ack — ``PAIRING_ABORT``'s receiver only drops a *non-confirmed*
    half, so it would keep a one-sided pairing. ``UNPAIR``'s receiver
    forgets the signer whatever the row's status (bound to the verified
    signature; the payload is empty, like a local unpair's)."""
    if pending_kind(inst) == PENDING_KIND_APPROVAL:
        return FederationEventType.UNPAIR, {}
    # The peer already holds the token (it is the one from the code); the
    # receiver binds it to the signer before dropping anything.
    payload: dict = {"reason": PAIRING_CANCEL_REASON}
    if tokens:
        payload = {"token": tokens[0], **payload}
    return FederationEventType.PAIRING_ABORT, payload


def _has_signed_channel(inst: RemoteInstance) -> bool:
    """Whether the half-pair holds the peer's identity key and our session
    key — what sealing and signing an envelope to it needs. A trust-relay
    request we sent has neither (the target never answered us)."""
    return bool(inst.remote_identity_pk and inst.key_self_to_remote)

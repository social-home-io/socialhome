"""Inbound federation handlers for the §11 pairing lifecycle.

Handles the RECEIVING side of every pairing event — the OUTBOUND side
is driven by :class:`~socialhome.federation.pairing_coordinator.PairingCoordinator`.

Events covered:

* ``PAIRING_INTRO`` — target receives a friend-of-friend introduction
  relayed via an intermediary. Published as
  :class:`PairingIntroReceived` so the admin UI can show the request.
* ``PAIRING_ACCEPT`` — initiator receives the accept envelope from
  the peer who scanned our QR. Upgrades local status → `PENDING_RECEIVED`
  and surfaces the SAS so the admin can confirm.
* ``PAIRING_CONFIRM`` — either side — peer finished verifying the SAS.
  Flip local status to `CONFIRMED`.
* ``PAIRING_ABORT`` — peer cancelled an in-progress handshake.
* ``UNPAIR`` — peer tore down an existing (confirmed) pairing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...domain.events import (
    DmContactRequested,
    PairingAborted,
    PairingAcceptReceived,
    PairingConfirmed,
    PairingIntroReceived,
    PeerUnpaired,
)
from ...domain.federation import FederationEventType, PairingStatus, RemoteInstance
from ...infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ...domain.federation import FederationEvent
    from ...federation.federation_service import FederationService
    from ...repositories.dm_contact_repo import AbstractDmContactRepo
    from ...repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class PairingInboundHandlers:
    """Six pairing-lifecycle inbound handlers registered on one federation
    service. (Five pairing events + the ``DM_CONTACT_REQUEST`` pre-pairing
    handshake, which lives in the same family.)
    """

    __slots__ = ("_bus", "_repo", "_dm_contact_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_repo: "AbstractFederationRepo",
        dm_contact_repo: "AbstractDmContactRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._repo = federation_repo
        self._dm_contact_repo = dm_contact_repo

    def attach_to(self, federation_service: "FederationService") -> None:
        """Register every handler on the service's event registry."""
        registry = federation_service._event_registry
        registry.register(FederationEventType.PAIRING_INTRO, self._on_intro)
        registry.register(FederationEventType.PAIRING_ACCEPT, self._on_accept)
        registry.register(FederationEventType.PAIRING_CONFIRM, self._on_confirm)
        registry.register(FederationEventType.PAIRING_ABORT, self._on_abort)
        registry.register(FederationEventType.UNPAIR, self._on_unpair)
        if self._dm_contact_repo is not None:
            registry.register(
                FederationEventType.DM_CONTACT_REQUEST,
                self._on_contact_request,
            )

    async def _on_intro(self, event: "FederationEvent") -> None:
        """Target side of §11.9: a peer introduced us to a new instance."""
        via = str(event.payload.get("via_instance_id") or "")
        message = str(event.payload.get("message") or "")[:500]
        if not via:
            log.debug("PAIRING_INTRO missing via_instance_id")
            return
        log.info(
            "PAIRING_INTRO received from=%s via=%s",
            event.from_instance,
            via,
        )
        await self._bus.publish(
            PairingIntroReceived(
                from_instance=event.from_instance,
                via_instance_id=via,
                message=message,
            )
        )

    async def _on_accept(self, event: "FederationEvent") -> None:
        """Initiator side — peer accepted our QR invite."""
        token = str(event.payload.get("token") or "")
        code = str(event.payload.get("verification_code") or "")
        if not token:
            log.debug("PAIRING_ACCEPT missing token")
            return
        # Upgrade the local PENDING_SENT row if we find it — the
        # PairingCoordinator created it during initiate().
        session = await self._repo.get_pairing(token)
        if session is None:
            log.warning(
                "PAIRING_ACCEPT for unknown token=%s from=%s",
                token,
                event.from_instance,
            )
            return
        await self._bus.publish(
            PairingAcceptReceived(
                from_instance=event.from_instance,
                token=token,
                verification_code=code,
            )
        )

    async def _on_confirm(self, event: "FederationEvent") -> None:
        """Peer confirmed the SAS — flip local status to CONFIRMED."""
        instance = await self._repo.get_instance(event.from_instance)
        if instance is None:
            log.debug(
                "PAIRING_CONFIRM from unknown instance=%s",
                event.from_instance,
            )
            return
        if instance.status is PairingStatus.CONFIRMED:
            return
        confirmed = RemoteInstance(
            id=instance.id,
            display_name=instance.display_name,
            remote_identity_pk=instance.remote_identity_pk,
            key_self_to_remote=instance.key_self_to_remote,
            key_remote_to_self=instance.key_remote_to_self,
            remote_webhook_url=instance.remote_webhook_url,
            local_webhook_id=instance.local_webhook_id,
            status=PairingStatus.CONFIRMED,
            source=instance.source,
            intro_relay_enabled=instance.intro_relay_enabled,
            proto_version=instance.proto_version,
            remote_pq_algorithm=instance.remote_pq_algorithm,
            remote_pq_identity_pk=instance.remote_pq_identity_pk,
            sig_suite=instance.sig_suite,
            relay_via=instance.relay_via,
            home_lat=instance.home_lat,
            home_lon=instance.home_lon,
            paired_at=instance.paired_at,
            created_at=instance.created_at,
            last_reachable_at=instance.last_reachable_at,
            unreachable_since=instance.unreachable_since,
        )
        await self._repo.save_instance(confirmed)
        await self._bus.publish(PairingConfirmed(instance_id=instance.id))

    async def _on_abort(self, event: "FederationEvent") -> None:
        """Peer cancelled the handshake — delete pending_pairings + surface."""
        token = str(event.payload.get("token") or "")
        reason = str(event.payload.get("reason") or "")[:200]
        if token:
            await self._repo.delete_pairing(token)
        instance = await self._repo.get_instance(event.from_instance)
        if instance is not None and instance.status is not PairingStatus.CONFIRMED:
            await self._repo.delete_instance(instance.id)
        await self._bus.publish(
            PairingAborted(
                instance_id=event.from_instance,
                reason=reason,
            )
        )

    async def _on_unpair(self, event: "FederationEvent") -> None:
        """Peer tore down a confirmed pairing. Purge the row."""
        instance = await self._repo.get_instance(event.from_instance)
        if instance is None:
            return
        await self._repo.delete_instance(instance.id)
        await self._bus.publish(PeerUnpaired(instance_id=event.from_instance))

    async def _on_contact_request(self, event: "FederationEvent") -> None:
        """§23.47: pre-pairing DM handshake — a remote user wants to start
        a conversation with a local user they haven't exchanged keys with
        yet. Persist a pending row + publish :class:`DmContactRequested`
        so :class:`NotificationService` can notify the recipient.
        """
        if self._dm_contact_repo is None:
            return
        p = event.payload
        requester_user_id = str(
            p.get("requester_user_id") or p.get("from_user_id") or "",
        )
        recipient_user_id = str(
            p.get("recipient_user_id") or p.get("to_user_id") or "",
        )
        requester_display_name = str(
            p.get("requester_display_name") or requester_user_id,
        )
        if not requester_user_id or not recipient_user_id:
            log.debug("DM_CONTACT_REQUEST missing requester/recipient user id")
            return
        try:
            await self._dm_contact_repo.save_request(
                from_user_id=requester_user_id,
                to_user_id=recipient_user_id,
            )
        except Exception as exc:  # pragma: no cover
            # The recipient's local user row may not exist yet — schema has
            # an FK to users.user_id. Log + drop rather than raise; the
            # admin can see this via operator logs.
            log.warning(
                "DM_CONTACT_REQUEST persistence failed (%s → %s): %s",
                requester_user_id,
                recipient_user_id,
                exc,
            )
            return
        await self._bus.publish(
            DmContactRequested(
                requester_user_id=requester_user_id,
                requester_display_name=requester_display_name,
                recipient_user_id=recipient_user_id,
            )
        )

"""Outbound federation for ``INSTANCE_CAPABILITIES_UPDATED``.

Fans out this build's :data:`~socialhome.domain.federation_capabilities.OURS`
``proto_version`` to every confirmed peer so they can gate optional
outbound fields on the version we actually run. Three trigger points:

1. **At app startup** — :meth:`publish` fans out to every peer that's
   confirmed *at boot time*. Covers the steady-state case.
2. **When a new pair lands** — :meth:`wire` subscribes to
   :class:`PairingConfirmed` so the freshly-confirmed peer gets a
   targeted announcement immediately, without waiting for the next
   restart. Without this, a peer paired *after* startup never learns
   our version and outbound senders that gate on ``peer_supports``
   would skip optional fields forever.
3. **Manually** — call :meth:`publish` after the operator changes the
   advertised set mid-run (rare; usually only on restart).
4. **GFS fallback switched on** — :class:`~socialhome.services
   .peer_gfs_relay_service.PeerGfsRelayService` calls :meth:`resend_to`
   when an admin turns the relay on for one paired household (v_54).
5. **Mesh-only space hosts** — :meth:`announce_to_mesh_host`, driven by
   the space sync scheduler's mesh sweep at startup and on its periodic
   tick: a host we reach only over the mesh holds no row for us and gets
   our version (plus identity key) as a claim over ``SPACE_ROUTED``.

A failed send to a single peer lands in the outbox retry queue; we
never raise.

**Key-wrap key for opted-in peers (v_54).** To a directly paired peer we
opted into the GFS relay with (``remote_instances.gfs_relay``), the
payload also carries our static key-wrap key — ``keywrap_pk`` /
``keywrap_sig`` / ``keywrap_suite``, the very material ``/gfs/info`` and a
GFS-reach pairing code carry, never a fresh key — so a pair made without
a GFS reach can switch the fallback on later. It rides the encrypted
payload like every other field; the receiver stores it only once it
verifies bound to our identity key. Additive: an older receiver ignores
it. Nobody else gets it — a peer we did not opt in with has no use for it.

**Every CONFIRMED peer, not just the social ones.** A household seated
from an invite link (``source = space_session``) is not a social peer —
it gets no DMs, no roster, no presence — but it does receive space
content, so it needs our version like anybody else. See
:meth:`CapabilitiesOutbound.confirmed_peers`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import PairingConfirmed
from ..domain.federation import FederationEventType, InstanceSource, PairingStatus
from ..domain.federation_capabilities import OURS as OUR_PROTO_VERSION
from ..federation.keywrap_seal import keywrap_wire_fields
from ..federation.mesh_member_claim import mesh_member_claim
from .peer_outbound import ConfirmedPeerBroadcaster

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class CapabilitiesOutbound(ConfirmedPeerBroadcaster):
    """Fan out ``INSTANCE_CAPABILITIES_UPDATED`` to every confirmed peer."""

    __slots__ = (
        "_federation",
        "_federation_repo",
        "_bus",
        "_keywrap_public_key",
        "_keywrap_sig",
    )

    # Narrow the broadcaster's optional ``_federation`` / ``_federation_repo``
    # — both required at construction, so the direct ``send_event`` and
    # ``get_local_identity`` accesses below are non-None.
    _federation: "FederationService"
    _federation_repo: "AbstractFederationRepo"

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        bus: "EventBus | None" = None,
        keywrap_public_key: bytes = b"",
        keywrap_sig: str = "",
    ) -> None:
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._bus = bus
        #: Our static key-wrap key + its identity binding signature, sent
        #: only to peers we opted into the GFS relay with (v_54). Empty
        #: means "not configured": the field is then never sent.
        self._keywrap_public_key = keywrap_public_key
        self._keywrap_sig = keywrap_sig

    async def confirmed_peers(self) -> list:
        """Every CONFIRMED peer — social **and** space-session.

        Overrides :meth:`ConfirmedPeerBroadcaster.confirmed_peers`, which
        reads the *social* list on purpose: DMs, the user roster,
        presence and the friends constellation must not treat "we share
        a space" as "we federate socially".

        ``proto_version`` is the exception, because it is not a social
        fact — it is the answer to "which optional fields may I put on
        the wire for this peer?", and it is exchanged in exactly one
        place: this event. A household seated from an invite link
        receives space posts, comments, roster events and sync chunks
        like any other peer, so leaving it off this fan-out froze its
        version at whatever it advertised at seat time and made every
        later ``peer_supports`` gate answer from stale data — silently
        withholding fields the peer could parse perfectly well. The
        event is already inbound-allow-listed for this source; the gap
        was only outbound.

        Fail-soft like the mixin it replaces: a repo error yields ``[]``
        rather than breaking startup.
        """
        repo = self._federation_repo
        try:
            peers = await repo.list_instances(status=PairingStatus.CONFIRMED.value)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("capabilities-outbound: list peers failed: %s", exc)
            return []
        own = getattr(self._federation, "own_instance_id", "")
        return [p for p in peers if getattr(p, "id", None) and p.id != own]

    def wire(self) -> None:
        """Subscribe to :class:`PairingConfirmed` so new pairs get a
        targeted announcement as soon as the handshake completes.

        Idempotent — the bus dedupes subscribers per (event_type, fn).
        Call once during app wiring after the bus is built.
        """
        if self._bus is None:
            return
        self._bus.subscribe(PairingConfirmed, self._on_pairing_confirmed)

    async def _on_pairing_confirmed(self, event: PairingConfirmed) -> None:
        """A new peer just became CONFIRMED — send our version to them.

        Targeted send (not a fan-out), so the cost is one envelope per
        pair regardless of how many peers we have. Fails-soft on send
        errors — the outbox retry layer redelivers, and the periodic
        startup fan-out covers any pair that drops to UNCONFIRMED and
        is later re-confirmed.
        """
        try:
            await self._send_to(event.instance_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "capabilities-outbound: on-pair send to %s failed: %s",
                event.instance_id,
                exc,
            )

    async def resend_to(self, instance_id: str) -> bool:
        """Re-advertise our ``proto_version`` to one peer on demand.

        Public entry point for the §319.6 ``INSTANCE_RESYNC_REQUEST``
        ``"capabilities"`` scope — a peer asks us to re-broadcast, and the
        resync handler delegates here rather than reaching into the private
        :meth:`_send_to`. Returns the same bool (``False`` for self / empty
        target).
        """
        return await self._send_to(instance_id)

    async def _send_to(self, instance_id: str) -> bool:
        own = getattr(self._federation, "_own_instance_id", "")
        if not instance_id or instance_id == own:
            return False
        # Carry our federated household name alongside the version so a
        # rename reaches already-paired peers (not just QR-time pairings).
        # Additive + fail-soft: older receivers ignore the field; if we
        # have no name yet (early bootstrap) the key is omitted entirely.
        local = await self._federation_repo.get_local_identity()
        payload: dict = {"proto_version": OUR_PROTO_VERSION}
        name = (local or {}).get("display_name")
        if isinstance(name, str) and name.strip():
            payload["display_name"] = name.strip()
        payload.update(await self._keywrap_fields_for(instance_id))
        await self._federation.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            payload=payload,
        )
        log.info(
            "capabilities-outbound: sent proto_version=%d to %s",
            OUR_PROTO_VERSION,
            instance_id,
        )
        return True

    async def _keywrap_fields_for(self, instance_id: str) -> dict[str, str]:
        """Our key-wrap fields for *instance_id*, or ``{}``.

        Only for a directly paired peer we opted into the GFS relay with:
        it needs the key to seal a relayed envelope (or a probe ack) to us.
        A link-joined household already has it from the invite bootstrap.
        """
        if not self._keywrap_public_key or not self._keywrap_sig:
            return {}
        peer = await self._federation_repo.get_instance(instance_id)
        if peer is None or not peer.gfs_relay:
            return {}
        if peer.source is not InstanceSource.MANUAL:
            return {}
        return keywrap_wire_fields(self._keywrap_public_key, self._keywrap_sig)

    async def announce_to_mesh_host(self, host_instance_id: str) -> bool:
        """Tell a space host we reach only over the mesh our version.

        Such a host holds no ``remote_instances`` row for us, so it never
        sees the confirmed-peer fan-out and could not deliver our writer
        cert / key / channel grant. The announcement carries the mesh
        claim (:mod:`~socialhome.federation.mesh_member_claim`) and rides
        :meth:`FederationService.send_with_mesh_fallback` — SPACE_ROUTED,
        sealed end to end to the host and origin-signed, so no relay reads
        or alters it. The host keeps it on our ``space_instances`` rows.

        Returns ``True`` when nothing more is needed (sent, or the host is
        a household we hold a row for — the ordinary path covers it) and
        ``False`` when the send failed and a later attempt should retry.
        """
        own = getattr(self._federation, "_own_instance_id", "")
        if not host_instance_id or host_instance_id == own:
            return True
        if await self._federation_repo.get_instance(host_instance_id) is not None:
            return True
        payload: dict = {
            "proto_version": OUR_PROTO_VERSION,
            **mesh_member_claim(self._federation.own_identity_pk),
        }
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=host_instance_id,
            event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            payload=payload,
        )
        ok = bool(getattr(result, "ok", False))
        log.info(
            "capabilities-outbound: mesh announcement to host %s %s",
            host_instance_id,
            "sent" if ok else "failed (will retry)",
        )
        return ok

    async def publish(self) -> int:
        """Tell every confirmed peer our current ``proto_version``.

        Returns the number of peers successfully notified (best-effort
        — a failed send to one peer doesn't block the others; the
        outbox retry layer redelivers anything that landed in the
        queue).
        """
        sent = 0
        for instance_id in await self.list_confirmed_peer_ids():
            try:
                if await self._send_to(instance_id):
                    sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "capabilities-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )
        log.info(
            "capabilities-outbound: notified %d peer(s), proto_version=%d",
            sent,
            OUR_PROTO_VERSION,
        )
        return sent

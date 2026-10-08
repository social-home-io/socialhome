"""Per-peer GFS fallback switch for an existing pairing (v_54).

A pair made with a plain ``url`` pairing code — or before the GFS relay
existed — has ``remote_instances.gfs_relay = 0`` and neither side holds the
other's key-wrap key, so the relay tier can never reach it. This service is
the admin's switch for one such pair:

* **On** — set our opt-in (``gfs_relay``), tell the peer our key-wrap key
  (it rides our ``INSTANCE_CAPABILITIES_UPDATED`` to that one peer, see
  :class:`~socialhome.services.capabilities_outbound.CapabilitiesOutbound`),
  then ask route discovery to probe the peer. The probe goes nowhere until
  the peer has turned the switch on too: route discovery needs the peer's
  key, and the peer's §24.11 relay opt-in gate refuses relayed traffic from
  a household it did not opt in with. When the peer's own switch flips, its
  capabilities announcement brings us its key
  (:class:`~socialhome.domain.events.PeerCapabilitiesAdvertised`
  ``keywrap_learned``) and we probe again at once.
* **Off** — clear our opt-in, forget this peer's relay routes and our
  probes in flight, and tell the peer: our capabilities now carry
  ``gfs_relay: false``, so it drops ITS routes to us. Without that, it
  would keep relaying to us for up to 72 h; the relay answers 202, so the
  envelopes count as delivered and are never queued, while our inbound
  gate refuses every one of them. The transport stops relaying to the
  peer at once. The keys stay: they are public, bound to the identity key,
  and needed again if the switch comes back on.

Only for a confirmed, directly paired household (``source = manual``): a
household met through an invite link already rides the GFS that introduced
it. Nothing about which GFS anyone uses goes on the wire —
``docs/protocol/gfs-relay.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..domain.events import PeerCapabilitiesAdvertised
from ..domain.federation import (
    GfsRelayNotAllowedError,
    GfsRelayPeerNotFoundError,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from ..repositories.federation_repo import AbstractFederationRepo

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)


class PeerGfsRelayService:
    """Turn the GFS relay fallback on or off for one paired household."""

    __slots__ = (
        "_federation_repo",
        "_send_capabilities",
        "_probe_peer",
        "_forget_probes",
        "_bus",
    )

    def __init__(
        self,
        *,
        federation_repo: AbstractFederationRepo,
        send_capabilities: Callable[[str], Awaitable[bool]],
        probe_peer: Callable[..., Awaitable[int]],
        forget_probes: Callable[[str], None],
        bus: "EventBus | None" = None,
    ) -> None:
        self._federation_repo = federation_repo
        #: Re-sends our capabilities to one peer —
        #: ``CapabilitiesOutbound.resend_to``. They carry our key-wrap key
        #: and ``gfs_relay: true`` while the opt-in is on, ``gfs_relay:
        #: false`` once it is off.
        self._send_capabilities = send_capabilities
        #: ``GfsRouteDiscoveryService.probe_peer`` — eligibility-gated and
        #: throttled per peer. An off → on switch passes ``throttled=False``.
        self._probe_peer = probe_peer
        #: ``GfsRouteDiscoveryService.forget_peer`` — drops our pending
        #: probe nonces (a late ack must not re-create a route).
        self._forget_probes = forget_probes
        self._bus = bus

    def wire(self) -> None:
        """Probe a peer as soon as its key-wrap key arrives (the other
        household just turned its switch on)."""
        if self._bus is None:
            return
        self._bus.subscribe(PeerCapabilitiesAdvertised, self._on_advertised)

    async def set_gfs_relay(
        self,
        instance_id: str,
        *,
        enabled: bool,
        set_by: str | None = None,
    ) -> RemoteInstance:
        """Switch the relay fallback for *instance_id*; return the row.

        Re-sending while already on is a retry, not a no-op: it repeats the
        key hand-over and asks for an ordinary, throttled probe. Only an
        off → on transition probes past the per-peer throttle, so repeated
        PATCHes cannot make us post a probe through every GFS each time.

        Raises :class:`GfsRelayPeerNotFoundError` for an unknown peer and
        :class:`GfsRelayNotAllowedError` for one that is not a confirmed,
        directly paired household.
        """
        peer = await self._federation_repo.get_instance(instance_id)
        if peer is None:
            raise GfsRelayPeerNotFoundError()
        if (
            peer.status is not PairingStatus.CONFIRMED
            or peer.source is not InstanceSource.MANUAL
        ):
            raise GfsRelayNotAllowedError()
        was_on = peer.gfs_relay
        await self._federation_repo.set_gfs_relay(instance_id, enabled=enabled)
        log.info(
            "GFS fallback for %s turned %s by %s",
            instance_id,
            "on" if enabled else "off",
            set_by or "<unknown>",
        )
        if enabled:
            await self._switch_on(instance_id, transition=not was_on)
        else:
            await self._switch_off(instance_id)
        updated = await self._federation_repo.get_instance(instance_id)
        return updated if updated is not None else peer

    async def _switch_on(self, instance_id: str, *, transition: bool) -> None:
        # Key first: the peer needs it to answer our probes (and to relay
        # to us at all). ``send_event`` falls back to the outbox, so a peer
        # that is offline right now still gets it.
        await self._tell_peer(instance_id)
        # An off → on switch always probes, and opens no throttle window:
        # it is usually unanswered (the other household has not switched on
        # yet), and must not swallow our probe-back once the other side
        # does. A repeat "on" is an ordinary, throttled probe.
        await self._probe(instance_id, throttled=not transition)

    async def _switch_off(self, instance_id: str) -> None:
        await self._forget_routes(instance_id)
        # ``gfs_relay: false`` in our capabilities: the peer drops its
        # routes to us instead of relaying into our closed gate.
        await self._tell_peer(instance_id)

    async def _forget_routes(self, instance_id: str) -> int:
        """Delete our routes to *instance_id* and our probes in flight to it
        (a late ack must not re-create a route; a stale throttle window must
        not swallow the probe-back once the fallback comes back on)."""
        routes = await self._federation_repo.list_gfs_routes(instance_id)
        for route in routes:
            await self._federation_repo.delete_gfs_route(
                instance_id, route.gfs_connection_id
            )
        self._forget_probes(instance_id)
        return len(routes)

    async def _tell_peer(self, instance_id: str) -> None:
        try:
            await self._send_capabilities(instance_id)
        except Exception:  # noqa: BLE001 — the switch is saved; retry later
            log.warning(
                "GFS fallback: could not tell %s about the switch",
                instance_id,
                exc_info=True,
            )

    async def _probe(self, instance_id: str, *, throttled: bool = True) -> None:
        try:
            if throttled:
                await self._probe_peer(instance_id)
            else:
                await self._probe_peer(instance_id, throttled=False)
        except Exception:  # noqa: BLE001 — discovery retries on its own timer
            log.warning(
                "GFS fallback: route probe for %s failed",
                instance_id,
                exc_info=True,
            )

    async def _on_advertised(self, event: PeerCapabilitiesAdvertised) -> None:
        if event.peer_gfs_relay is False:
            # The other household switched its fallback off: its relay
            # opt-in gate refuses our relayed envelopes, and the relay would
            # answer 202 to each — counted delivered, never queued. Drop the
            # routes so our traffic goes back to RTC / HTTPS / the outbox.
            # Our own opt-in stays (our admin's switch); its next switch-on
            # probe restores the routes.
            dropped = await self._forget_routes(event.instance_id)
            if dropped:
                log.info(
                    "GFS fallback: %s switched it off — dropped %d route(s)",
                    event.instance_id,
                    dropped,
                )
            return
        # ``probe_peer`` checks our own opt-in, so a key from a household we
        # did not switch on for is stored and nothing is sent.
        if event.keywrap_learned:
            await self._probe(event.instance_id)


__all__ = ["PeerGfsRelayService"]

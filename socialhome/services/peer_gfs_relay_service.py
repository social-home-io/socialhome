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
* **Off** — clear our opt-in and forget this peer's relay routes. The
  transport stops relaying to it at once and the inbound gate refuses any
  relayed envelope from it. The keys stay: they are public, bound to the
  identity key, and needed again if the switch comes back on.

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

    __slots__ = ("_federation_repo", "_send_keywrap", "_probe_peer", "_bus")

    def __init__(
        self,
        *,
        federation_repo: AbstractFederationRepo,
        send_keywrap: Callable[[str], Awaitable[bool]],
        probe_peer: Callable[..., Awaitable[int]],
        bus: "EventBus | None" = None,
    ) -> None:
        self._federation_repo = federation_repo
        #: Re-sends our capabilities (with our key-wrap key, now that the
        #: opt-in is on) to one peer — ``CapabilitiesOutbound.resend_to``.
        self._send_keywrap = send_keywrap
        #: ``GfsRouteDiscoveryService.probe_peer`` — eligibility-gated and
        #: throttled per peer, so calling it more than needed is harmless.
        #: The admin's own switch passes ``hold_throttle=False`` (see there).
        self._probe_peer = probe_peer
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
        key hand-over and asks for a probe (throttled per peer).

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
        await self._federation_repo.set_gfs_relay(instance_id, enabled=enabled)
        log.info(
            "GFS fallback for %s turned %s by %s",
            instance_id,
            "on" if enabled else "off",
            set_by or "<unknown>",
        )
        if enabled:
            await self._switch_on(instance_id)
        else:
            await self._drop_routes(instance_id)
        updated = await self._federation_repo.get_instance(instance_id)
        return updated if updated is not None else peer

    async def _switch_on(self, instance_id: str) -> None:
        # Key first: the peer needs it to answer our probes (and to relay
        # to us at all). ``send_event`` falls back to the outbox, so a peer
        # that is offline right now still gets it.
        try:
            await self._send_keywrap(instance_id)
        except Exception:  # noqa: BLE001 — the switch is saved; retry later
            log.warning(
                "GFS fallback: could not send our key-wrap key to %s",
                instance_id,
                exc_info=True,
            )
        # Usually unanswered (the other household has not switched on yet):
        # it must not open the per-peer throttle window that would swallow
        # our probe-back once the other side does.
        await self._probe(instance_id, hold_throttle=False)

    async def _drop_routes(self, instance_id: str) -> None:
        for route in await self._federation_repo.list_gfs_routes(instance_id):
            await self._federation_repo.delete_gfs_route(
                instance_id, route.gfs_connection_id
            )

    async def _probe(self, instance_id: str, *, hold_throttle: bool = True) -> None:
        try:
            if hold_throttle:
                await self._probe_peer(instance_id)
            else:
                await self._probe_peer(instance_id, hold_throttle=False)
        except Exception:  # noqa: BLE001 — discovery retries on its own timer
            log.warning(
                "GFS fallback: route probe for %s failed",
                instance_id,
                exc_info=True,
            )

    async def _on_advertised(self, event: PeerCapabilitiesAdvertised) -> None:
        # ``probe_peer`` checks our own opt-in, so a key from a household we
        # did not switch on for is stored and nothing is sent.
        if event.keywrap_learned:
            await self._probe(event.instance_id)


__all__ = ["PeerGfsRelayService"]

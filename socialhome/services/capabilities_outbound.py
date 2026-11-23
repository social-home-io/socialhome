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
5. **Mesh-only member households** — :meth:`announce_over_mesh`: a
   household of one of our spaces that we reach only over the mesh holds
   no row for us (nor we for it) and gets our version (plus identity key)
   as a claim over ``SPACE_ROUTED``. Driven by the space sync scheduler's
   mesh sweep (once per process, at startup and on its periodic tick) and,
   right away, by :meth:`FederationService.record_mesh_member_claim` when
   such a household's own claim raises its version here.

A failed send to a single peer lands in the outbox retry queue; we
never raise.

**Relay opt-in and key-wrap key (v_54).** To a directly paired peer the
payload carries ``gfs_relay`` — our opt-in (a ``false`` makes the peer drop
its relay routes to us). Opted in, it also carries our static key-wrap key — ``keywrap_pk`` /
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

from ..domain.events import (
    CpProtectionDisabled,
    CpProtectionEnabled,
    PairingConfirmed,
    UserDeprovisioned,
    UserPreferencesPatched,
    UserProfileUpdated,
    UserProvisioned,
)
from ..domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from ..domain.federation_capabilities import OURS as OUR_PROTO_VERSION
from ..domain.federation_capabilities import FederationCapability
from ..domain.moment import MOMENT_UNGATED_HOPS
from ..federation.keywrap_seal import keywrap_wire_fields
from ..federation.mesh_member_claim import mesh_member_claim
from .peer_outbound import ConfirmedPeerBroadcaster

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.moment_repo import AbstractMomentRepo

log = logging.getLogger(__name__)

#: The latest-only outbox key of every capabilities send to a peer (local,
#: never on the wire): an offline peer keeps one queued row — the newest —
#: so an older envelope never lands after a newer look.
CAPABILITIES_OUTBOX_KEY: str = "capabilities"


def advertised_look(look: int) -> int:
    """How far our people look for moments, as we say it (v_96 decision C):
    never below :data:`MOMENT_UNGATED_HOPS` — a relay only uses a look past
    it (hops 1–3 go to every household), so a smaller one would reveal what
    our people chose (or that they are protected) for nothing."""
    return max(look, MOMENT_UNGATED_HOPS)


class CapabilitiesOutbound(ConfirmedPeerBroadcaster):
    """Fan out ``INSTANCE_CAPABILITIES_UPDATED`` to every confirmed peer."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_keywrap_public_key",
        "_keywrap_sig",
        "_look",
        "_moment_repo",
    )

    # Narrow the broadcaster's optional ``_federation`` / ``_federation_repo``
    # — both required at construction, so the direct ``send_event`` and
    # ``get_local_identity`` accesses below are non-None.
    _federation: FederationService
    _federation_repo: AbstractFederationRepo

    def __init__(
        self,
        *,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
        bus: EventBus | None = None,
        keywrap_public_key: bytes = b"",
        keywrap_sig: str = "",
        moment_repo: AbstractMomentRepo | None = None,
    ) -> None:
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._bus = bus
        #: v_96 decision C — where how far our people look is read
        #: (``moment_look_hops``). ``None``: the field is never sent.
        self._moment_repo = moment_repo
        #: The look we last advertised (``None``: not computed yet). Kept in
        #: memory: the startup publish always carries the current one.
        self._look: int | None = None
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
            log.debug(
                "capabilities-outbound: list peers failed: %s", exc, exc_info=True
            )
            return []
        own = getattr(self._federation, "own_instance_id", "")
        return [
            p
            for p in peers
            if getattr(p, "id", None)
            and p.id != own
            # v_89 — a member seated from a host's directory learns our
            # version from our member card (the host passes it on), not
            # from a fan-out to every such seat on every start.
            and not (
                getattr(p, "source", None) is InstanceSource.SPACE_SESSION
                and not getattr(p, "relay_via", None)
            )
        ]

    def wire(self) -> None:
        """Subscribe to :class:`PairingConfirmed` so new pairs get a
        targeted announcement as soon as the handshake completes.

        Idempotent — the bus dedupes subscribers per (event_type, fn).
        Call once during app wiring after the bus is built.
        """
        if self._bus is None:
            return
        self._bus.subscribe(PairingConfirmed, self._on_pairing_confirmed)
        if self._moment_repo is None:
            return
        # v_96 decision C: what may change how far our people look — a
        # reactivated account comes back as ``UserProfileUpdated``.
        for look_input in (
            UserPreferencesPatched,
            UserProvisioned,
            UserProfileUpdated,
            UserDeprovisioned,
            CpProtectionEnabled,
            CpProtectionDisabled,
        ):
            self._bus.subscribe(look_input, self._on_look_input)

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
                exc_info=True,
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

    async def _send_to(self, instance_id: str, *, look: int | None = None) -> bool:
        """Send our capabilities to one peer. *look*: how far our people
        look for moments, when the caller (a fan-out) already read it."""
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
        # The peer's row, read once for the fields only a directly paired
        # peer gets (none configured: never read).
        peer = (
            await self._federation_repo.get_instance(instance_id)
            if (self._keywrap_public_key and self._keywrap_sig)
            or self._moment_repo is not None
            else None
        )
        payload.update(self._relay_fields_for(peer))
        payload.update(await self._moment_look_fields_for(peer, look))
        await self._federation.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            payload=payload,
            outbox_key=CAPABILITIES_OUTBOX_KEY,
        )
        log.info(
            "capabilities-outbound: sent proto_version=%d to %s",
            OUR_PROTO_VERSION,
            instance_id,
        )
        return True

    async def _moment_look_fields_for(
        self, peer: RemoteInstance | None, look: int | None
    ) -> dict[str, object]:
        """``{"moment_look_hops": N}`` for a directly paired peer (v_96,
        decision C) — how far our people look for moments (*look*, else read
        now), so its relays send us a hop-4 or hop-5 copy only when someone
        here looks that far. One number, to paired households only: never to
        a household we only share a space with (``space_session``), never
        over the mesh or to a GFS. Additive: an older receiver ignores it."""
        if (
            self._moment_repo is None
            or peer is None
            or peer.source is not InstanceSource.MANUAL
        ):
            return {}
        if look is None:
            look = advertised_look(await self._moment_repo.moment_look_hops())
        return {"moment_look_hops": look}

    async def refresh_moment_look(self) -> int:
        """Recompute how far our people look for moments and, when the
        number changed since we last advertised it, re-send our capabilities
        to every paired v_96 household (an older one ignores the field;
        once it reaches v_96 its startup capability resync asks us again).
        Only the advertised value counts (:func:`advertised_look`): a change
        below :data:`MOMENT_UNGATED_HOPS` sends nothing. Before the startup
        :meth:`publish` (``_look`` unset) nothing is sent — it carries the
        current value. Returns how many households were told (0: nothing
        changed)."""
        if self._moment_repo is None or self._look is None:
            return 0
        look = advertised_look(await self._moment_repo.moment_look_hops())
        if look == self._look:
            return 0
        self._look = look
        sent = 0
        for peer in await self.confirmed_peers():
            if (
                peer.source is not InstanceSource.MANUAL
                or peer.proto_version < FederationCapability.MIN_FOR_MOMENT_REACH
            ):
                continue
            try:
                if await self._send_to(peer.id, look=look):
                    sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "capabilities-outbound: look send to %s failed: %s",
                    peer.id,
                    exc,
                    exc_info=True,
                )
        log.info(
            "capabilities-outbound: moment_look_hops -> %d, told %d peer(s)",
            look,
            sent,
        )
        return sent

    async def _on_look_input(self, event: object) -> None:
        """A person here was added or removed, protected or no longer, or
        patched their preferences: how far we look may have changed."""
        if isinstance(event, UserPreferencesPatched) and "moments" not in event.keys:
            return
        await self.refresh_moment_look()

    def _relay_fields_for(self, peer: RemoteInstance | None) -> dict[str, object]:
        """Our GFS-relay fields for *peer*, or ``{}`` (v_54).

        Directly paired peers only (a link-joined household got our key
        from the invite bootstrap and rides the relay by construction):

        * opted in — ``gfs_relay: true`` plus our key-wrap key, which the
          peer needs to seal a relayed envelope (or a probe ack) to us;
        * not opted in — ``gfs_relay: false``, so a peer that still holds
          routes to us drops them: our inbound gate refuses its relayed
          envelopes, while the relay answers 202 to each one, so they would
          count as delivered and never be queued.
        """
        if not self._keywrap_public_key or not self._keywrap_sig:
            return {}
        if peer is None or peer.source is not InstanceSource.MANUAL:
            return {}
        if not peer.gfs_relay:
            return {"gfs_relay": False}
        return {
            "gfs_relay": True,
            **keywrap_wire_fields(self._keywrap_public_key, self._keywrap_sig),
        }

    async def announce_over_mesh(self, instance_id: str) -> bool:
        """Tell a member household of one of our spaces that we reach only
        over the mesh our version.

        Neither side holds a ``remote_instances`` row for the other, so
        neither sees the confirmed-peer fan-out: a host could not deliver
        our writer cert / key / channel grant, and any member household's
        version-gated space broadcast (``broadcast_to_space_members(…,
        min_proto_version=N)``) skips a household whose version it does not
        know — a mesh-only member's reaction never reached its host. The
        announcement carries the mesh claim
        (:mod:`~socialhome.federation.mesh_member_claim`) and rides
        :meth:`FederationService.send_with_mesh_fallback` — SPACE_ROUTED,
        sealed end to end to the target and origin-signed, so no relay reads
        or alters it. The receiver keeps it on our ``space_instances`` rows.

        Returns ``True`` when nothing more is needed (sent, or the target is
        a household we hold a row for — the ordinary path covers it) and
        ``False`` when the send failed and a later attempt should retry.
        """
        own = getattr(self._federation, "_own_instance_id", "")
        if not instance_id or instance_id == own:
            return True
        if await self._federation_repo.get_instance(instance_id) is not None:
            return True
        payload: dict = {
            "proto_version": OUR_PROTO_VERSION,
            **mesh_member_claim(self._federation.own_identity_pk),
        }
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=instance_id,
            event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
            payload=payload,
        )
        ok = bool(getattr(result, "ok", False))
        # A failure is the scheduler's to report (once, when it slows down):
        # a household no route reaches fails on every sweep.
        log.log(
            logging.INFO if ok else logging.DEBUG,
            "capabilities-outbound: mesh announcement to %s %s",
            instance_id,
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
        if self._moment_repo is not None:
            # v_96: the startup announcement carries how far we look now.
            self._look = advertised_look(await self._moment_repo.moment_look_hops())
        for instance_id in await self.list_confirmed_peer_ids():
            try:
                if await self._send_to(instance_id, look=self._look):
                    sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "capabilities-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                    exc_info=True,
                )
        log.info(
            "capabilities-outbound: notified %d peer(s), proto_version=%d",
            sent,
            OUR_PROTO_VERSION,
        )
        return sent

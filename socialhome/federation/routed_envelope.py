"""Source-routed envelope handler for federation mesh forwarding (§D2 PR 2).

:data:`FederationEventType.SPACE_ROUTED` wraps an arbitrary inner
event_type + payload and threads it through a pre-computed
source-route. The path is computed once at the origin via
:class:`RouteDiscoveryService`; every hop reads ``path[position+1]``
to find its next hop, bumps ``position``, and re-ships. When the
hop where ``position+1 == len(path)-1 and path[-1] == self`` lands
the envelope, it unwraps the inner event and dispatches it through
the same registry the federation service uses for direct events —
tagged with ``origin_instance_id = path[0]`` so the inner handler
sees the *real* source, not the relay it arrived from.

This is one envelope for every mesh use case: invite-redeem (PR 1
gets it transparently in PR 2), space content fanout to non-paired
households, future event types — none of them need their own
``_ROUTED`` variant.

End-to-end encryption (§D2 PR 2): every inner payload is
AES-256-GCM-sealed with a per-route, ephemeral X25519+HKDF key
derived between origin and target — relays only ever see the
``sealed`` blob and the routing fields (``route_id``, ``path``,
``position``, ``direction``, ``inner_event_type``). The forward
leg's ephemeral keypairs are cached briefly so the reply leg
(direction="reply") can reuse them to seal the ACK back to the
origin without a second discovery. See
:mod:`socialhome.federation.routed_crypto` for the wire shape and
threat model.

Cycle / loop guards:

* ``route_id`` nonce dedup keyed on the envelope (TTL
  ``seen_ttl_s``). A relay that already saw a route_id drops the
  envelope silently — keeps a misconfigured path from ping-ponging.
* If ``self`` appears in ``path[position+1:]`` (forward leg has us
  again), the envelope is dropped: relaying it would re-enter a
  cycle the discovery should have caught.

ACK / response routing: when an inner handler runs
``send_routed_reply`` with the ``route_id`` it received (carried on
:attr:`FederationEvent.routed_route_id`), the response travels back
through the reverse of the original path with ``direction="reply"``
and the seal uses the target→origin key.

Route-stale nack (:data:`FederationEventType.SPACE_ROUTE_STALE`):
the target's ephemeral private half lives only in RAM, so after a
restart every envelope sealed under the old pub is undecryptable —
while origins keep sealing under it for the rest of their route-cache
window, believing delivery succeeded. When the forward-leg unwrap
finds no private half, the target signs ``(route_id, stale_eph_pk)``
with its identity key and ships the nack to the previous hop; each
relay walks it back one hop (``position`` names the hop that SENT the
nack, mirroring ``SPACE_ROUTED``) until it reaches ``path[0]``.
Relays are opaque forwarders — structural checks only, no signature
verification; the origin is the enforcement point. Gated per hop on
:data:`FederationCapability.MIN_FOR_ROUTE_STALE_NACK` so a pre-v_28
peer sees exactly today's silent drop. Payload is routing/validation
fields only — no content, per the encryption-first rule.

At the origin (:meth:`SpaceRoutedHandler._on_route_stale_at_origin`)
every ``send_routed`` leaves a :class:`_PendingRouted` record (target,
the identity pk pinned at discovery, the inner event) for the ephemeral
window. A nack is honoured only if it names a route_id we sent, ends at
that record's target, carries the pinned identity pk, and verifies
under it — then the route is invalidated, rediscovered, and the inner
event retransmitted exactly once (a nacked retry gives up).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..crypto import derive_instance_id
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from . import routed_crypto
from .route_discovery import cap_by_expiry

if TYPE_CHECKING:
    from ..repositories.federation_repo import AbstractFederationRepo
    from .federation_service import FederationService
    from .route_discovery import RouteDiscoveryService

log = logging.getLogger(__name__)

#: Re-dispatch callback signature. The handler accepts a synthesised
#: :class:`FederationEvent` whose ``from_instance`` is the *origin*
#: of the routed envelope (``path[0]``) and ``routed_path`` carries
#: the full source-route — so the inner handler can decide whether
#: to ship a response directly or via SPACE_ROUTED with the
#: reverse path.
EventDispatcher = Callable[[FederationEvent], Awaitable[None]]

#: Lookup for the target-side ephemeral private half corresponding
#: to a given ephemeral public — supplied by
#: :class:`RouteDiscoveryService.lookup_target_eph_priv`. Wired in as
#: a callable rather than the service itself so the handler doesn't
#: import the discovery class (and so tests can pass a tiny lambda).
TargetEphLookup = Callable[[str], str | None]

#: TTL on the origin-side / target-side ephemeral caches kept by the
#: routed handler. Long enough for the ACK to flow back (the redeem
#: timeout is ~10 s) but short enough that a forgotten route_id
#: doesn't accumulate state. Matches ``_seen_routes``.
_DEFAULT_EPH_TTL_S: float = 60.0

#: Hard ceiling on the number of hops in a SPACE_ROUTED ``path``. Route
#: discovery (``route_discovery.RouteDiscoveryService``) produces paths of at
#: most ``max_hops`` (default 3) intermediate hops + the target, i.e. ≤ 4
#: nodes. We accept a generous margin but reject anything longer: a relay
#: trusts the (confirmed-peer-signed) ``path`` it receives, so without a cap a
#: misbehaving confirmed peer could submit an arbitrarily long distinct-node
#: path and have each hop forward it once. The cycle guard + per-hop §24.11
#: gate already bound this, but a length cap closes the amplification tail.
_MAX_ROUTED_PATH_LEN: int = 8

#: Largest inner payload (compact JSON, bytes) the origin keeps around for a
#: possible ``SPACE_ROUTE_STALE`` retransmit. Anything bigger is a media
#: chunk: those ride the durable ``space_media_outbox``, which re-discovers
#: the route on its own next retry, so retaining them here would only
#: duplicate bytes in RAM. An oversized send still gets a pending record
#: (with ``inner_payload=None``) so the nack can invalidate the route.
_MAX_RETAINED_INNER_BYTES: int = 64 * 1024

#: Ceiling on ``_pending_routed`` entries. Each is bounded by
#: ``_MAX_RETAINED_INNER_BYTES``, so this caps the retained-inner memory at
#: ~128 MiB worst case while comfortably covering the sends a household
#: makes inside one ephemeral window (60 s). Eviction is oldest-expiry-first
#: via :func:`cap_by_expiry`, same as every other dict in this handler.
_MAX_PENDING_ROUTED: int = 2000


@dataclass(slots=True, frozen=True)
class _PendingRouted:
    """Handler-local DTO — what the origin retains per ``send_routed`` so a
    verified ``SPACE_ROUTE_STALE`` nack can invalidate + retransmit.

    Lives here (not in ``domain/``) for the same reason
    ``route_discovery._CachedRoute`` does: it is in-RAM bookkeeping that
    never leaves :class:`SpaceRoutedHandler` — no wire shape, no row shape,
    no consumer outside this module.
    """

    #: ``path[-1]`` of the send; a nack must end at this instance.
    target_instance_id: str
    #: Hex identity pk pinned at discovery (``""`` when the caller had
    #: none) — the key a nack's ``target_identity_pk`` must equal.
    target_identity_pk: str
    inner_event_type: FederationEventType
    #: Copy of the inner payload, or ``None`` when it exceeded
    #: :data:`_MAX_RETAINED_INNER_BYTES` (invalidate-only, no retransmit).
    inner_payload: dict | None
    #: True when this send IS the one retransmit — a nack for it gives up.
    is_retry: bool
    expires_at: float


class SpaceRoutedHandler:
    """Wraps + unwraps :data:`FederationEventType.SPACE_ROUTED`."""

    __slots__ = (
        "_federation",
        "_federation_repo",
        "_dispatcher",
        "_target_eph_lookup",
        "_seen_ttl_s",
        "_eph_ttl_s",
        "_seen_routes",
        "_seen_nacks",
        "_origin_eph_state",
        "_reply_eph_state",
        "_pending_routed",
        "_route_service",
    )

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        event_dispatcher: EventDispatcher,
        target_eph_lookup: TargetEphLookup,
        seen_ttl_s: float = 60.0,
        eph_ttl_s: float = _DEFAULT_EPH_TTL_S,
    ) -> None:
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._dispatcher = event_dispatcher
        self._target_eph_lookup = target_eph_lookup
        self._seen_ttl_s = seen_ttl_s
        self._eph_ttl_s = eph_ttl_s
        #: ``route_id`` → wall-clock expiry. Stops a misbehaving
        #: chain from cycling forever even if the discovery layer
        #: produced an invalid path.
        self._seen_routes: dict[str, float] = {}
        #: ``route_id`` → wall-clock expiry for SPACE_ROUTE_STALE nacks
        #: we already forwarded / consumed. A nack visits each hop of
        #: a path at most once; a replay is a no-op. Same TTL + cap as
        #: ``_seen_routes``.
        self._seen_nacks: dict[str, float] = {}
        #: Origin-side: ``route_id`` →
        #: ``(origin_eph_priv_b64, origin_eph_pub_b64, expires_at)``.
        #: Set on ``send_routed``; consumed on the matching
        #: reply-leg unwrap so the origin can decrypt the ACK.
        self._origin_eph_state: dict[str, tuple[str, str, float]] = {}
        #: Target-side: ``route_id`` →
        #: ``(target_eph_priv_b64, target_eph_pub_b64,
        #:   origin_eph_pub_b64, reply_path, expires_at)``.
        #: Set on forward-leg unwrap; consumed by ``send_routed_reply``
        #: so the target can seal the ACK back to the origin without a
        #: second discovery probe.
        self._reply_eph_state: dict[str, tuple[str, str, str, list[str], float]] = {}
        #: Origin-side: ``route_id`` → what we need to honour a
        #: ``SPACE_ROUTE_STALE`` nack for that send. TTL ``eph_ttl_s`` —
        #: a nack has to arrive inside the ephemeral window anyway — and
        #: capped at :data:`_MAX_PENDING_ROUTED`.
        self._pending_routed: dict[str, _PendingRouted] = {}
        #: Set by :meth:`attach_route_service` (called from
        #: ``FederationService.attach_mesh``). ``None`` → a verified nack
        #: is consumed but nothing is invalidated or retransmitted.
        self._route_service: RouteDiscoveryService | None = None

    # ── Attach ─────────────────────────────────────────────────────────

    def attach_route_service(self, route_service: "RouteDiscoveryService") -> None:
        """Give the handler the discovery service so a verified
        ``SPACE_ROUTE_STALE`` nack can invalidate + rediscover the route."""
        self._route_service = route_service

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_ROUTED,
            self._on_routed,
        )
        registry.register(
            FederationEventType.SPACE_ROUTE_STALE,
            self._on_route_stale,
        )

    # ── Public API ─────────────────────────────────────────────────────

    async def send_routed(
        self,
        *,
        path: list[str],
        target_eph_pk_b64: str,
        inner_event_type: FederationEventType,
        inner_payload: dict,
        target_identity_pk: str = "",
        is_retry: bool = False,
    ) -> str:
        """Ship ``inner_event_type`` along ``path`` (origin → … → target).

        ``path[0]`` MUST equal the local instance id; ``path[-1]`` is
        the ultimate target. ``target_eph_pk_b64`` is the target's
        ephemeral X25519 public key — obtained from
        :meth:`RouteDiscoveryService.discover_route` — under which the
        inner payload is sealed.

        ``target_identity_pk`` is the hex identity pk the discovery
        service pinned for ``path[-1]``
        (:meth:`RouteDiscoveryService.cached_target_identity_pk`); a
        ``SPACE_ROUTE_STALE`` nack for this send must carry exactly that
        key. ``""`` (legacy callers) falls back to the ``derive_instance_id``
        binding alone. ``is_retry`` marks the single retransmit the origin
        makes after a nack, so a nack for the retransmit gives up instead
        of looping.

        Returns the generated ``route_id`` so the caller can correlate
        the eventual reply (the reply arrives via the dispatcher with
        :attr:`FederationEvent.routed_route_id` set to this value).
        """
        if len(path) < 2:
            raise ValueError(
                "SpaceRoutedHandler.send_routed: path must contain at"
                " least origin and target",
            )
        self_id = self._federation.own_instance_id
        if path[0] != self_id:
            raise ValueError(
                "SpaceRoutedHandler.send_routed: path[0] must equal own instance id",
            )
        route_id = secrets.token_hex(16)
        # Mint origin ephemeral, seal the inner payload, stash priv
        # for the reply leg.
        origin_priv_b64, origin_pub_b64 = routed_crypto.generate_ephemeral_keypair()
        inner_payload_json = json.dumps(
            inner_payload,
            separators=(",", ":"),
            sort_keys=True,
        )
        sealed = routed_crypto.seal_inner_payload(
            inner_payload_json=inner_payload_json,
            origin_eph_priv_b64=origin_priv_b64,
            origin_eph_pub_b64=origin_pub_b64,
            target_eph_pub_b64=target_eph_pk_b64,
            route_id=route_id,
            inner_event_type=inner_event_type.value,
        )
        now = time.monotonic()
        self._origin_eph_state[route_id] = (
            origin_priv_b64,
            origin_pub_b64,
            now + self._eph_ttl_s,
        )
        # Retain what a SPACE_ROUTE_STALE nack needs. Measured on the
        # compact JSON already built for the seal (the same bytes that go
        # on the wire); over the cap → invalidate-only record.
        retained: dict | None = dict(inner_payload)
        if len(inner_payload_json.encode()) > _MAX_RETAINED_INNER_BYTES:
            retained = None
        self._pending_routed[route_id] = _PendingRouted(
            target_instance_id=path[-1],
            target_identity_pk=target_identity_pk,
            inner_event_type=inner_event_type,
            inner_payload=retained,
            is_retry=is_retry,
            expires_at=now + self._eph_ttl_s,
        )
        # Origin-side dedup: if this same route_id loops back to us
        # (shouldn't happen with a valid path, but defensive), drop.
        self._seen_routes[route_id] = now + self._seen_ttl_s
        await self._federation.send_event(
            to_instance_id=path[1],
            event_type=FederationEventType.SPACE_ROUTED,
            payload={
                "route_id": route_id,
                "path": list(path),
                "position": 0,
                "direction": "forward",
                "inner_event_type": inner_event_type.value,
                "sealed": sealed,
            },
        )
        return route_id

    async def send_routed_reply(
        self,
        *,
        route_id: str,
        inner_event_type: FederationEventType,
        inner_payload: dict,
    ) -> None:
        """Ship a reply for an inbound forward-leg envelope.

        Looks up the cached target-side ephemeral state by
        ``route_id`` (populated when the forward leg unwrapped at us),
        seals ``inner_payload`` under the target→origin directional
        key, and ships SPACE_ROUTED back along the reverse of the
        forward path with ``direction="reply"``.

        Raises :class:`LookupError` if no reply state exists for
        ``route_id`` (missing entirely or expired).
        """
        now = time.monotonic()
        self._prune_eph_state(now)
        entry = self._reply_eph_state.pop(route_id, None)
        if entry is None:
            raise LookupError(
                f"SpaceRoutedHandler.send_routed_reply: no reply state for"
                f" route_id={route_id[:8]} (expired or never received)",
            )
        target_priv_b64, target_pub_b64, origin_pub_b64, reply_path, _exp = entry
        inner_payload_json = json.dumps(
            inner_payload,
            separators=(",", ":"),
            sort_keys=True,
        )
        sealed = routed_crypto.seal_reply_payload(
            inner_payload_json=inner_payload_json,
            target_eph_priv_b64=target_priv_b64,
            target_eph_pub_b64=target_pub_b64,
            origin_eph_pub_b64=origin_pub_b64,
            route_id=route_id,
            inner_event_type=inner_event_type.value,
        )
        # Mark the reply route_id as seen so a re-entrance is dropped.
        self._seen_routes[route_id] = now + self._seen_ttl_s
        if len(reply_path) < 2:
            # Defensive: a one-element reverse path (origin == target)
            # would mean we never had to leave the box. Shouldn't
            # happen — the forward leg requires len(path) >= 2.
            raise LookupError(
                f"SpaceRoutedHandler.send_routed_reply: reply_path too short"
                f" for route_id={route_id[:8]}",
            )
        await self._federation.send_event(
            to_instance_id=reply_path[1],
            event_type=FederationEventType.SPACE_ROUTED,
            payload={
                "route_id": route_id,
                "path": list(reply_path),
                "position": 0,
                "direction": "reply",
                "inner_event_type": inner_event_type.value,
                "sealed": sealed,
            },
        )

    # ── Inbound handler ────────────────────────────────────────────────

    async def _on_routed(self, event: FederationEvent) -> None:
        """Forward or unwrap a routed envelope.

        Defensive validation first (path well-formed, position
        sane), then loop / cycle dedup, then either:

        * Unwrap if ``self == path[position+1]`` and that's the last
          element → unseal inner payload + dispatch with origin attribution.
        * Forward to ``path[position+1]`` otherwise — propagating the
          encrypted ``sealed`` blob untouched (relays never decrypt).
        """
        p = event.payload
        route_id = str(p.get("route_id") or "")
        path_raw = p.get("path")
        path = [str(h) for h in path_raw] if isinstance(path_raw, list) else []
        position_raw = p.get("position")
        if position_raw is None:
            return
        try:
            position = int(position_raw)
        except TypeError, ValueError:
            return
        inner_type_raw = str(p.get("inner_event_type") or "")
        sealed_raw = p.get("sealed")
        # Default direction to "forward" for forward-compat; new
        # senders always set it explicitly.
        direction = str(p.get("direction") or "forward")
        if not route_id or not path:
            log.debug("SPACE_ROUTED: missing route_id/path; dropping")
            return
        if len(path) > _MAX_ROUTED_PATH_LEN:
            # A relay trusts the path it's handed; cap its length so a
            # misbehaving confirmed peer can't submit an over-long chain.
            log.warning(
                "SPACE_ROUTED route_id=%s: path too long (%d > %d); dropping",
                route_id[:8],
                len(path),
                _MAX_ROUTED_PATH_LEN,
            )
            return
        if not isinstance(sealed_raw, dict):
            log.debug(
                "SPACE_ROUTED route_id=%s: missing/non-dict sealed blob; dropping",
                route_id[:8],
            )
            return
        if direction not in ("forward", "reply"):
            log.warning(
                "SPACE_ROUTED route_id=%s: unknown direction=%r; dropping",
                route_id[:8],
                direction,
            )
            return
        if position < 0 or position + 1 >= len(path):
            # Position points past the end of the path — the envelope
            # was already at the target or is malformed.
            log.debug(
                "SPACE_ROUTED: position=%s out of range for path_len=%s",
                position,
                len(path),
            )
            return

        try:
            inner_type = FederationEventType(inner_type_raw)
        except ValueError:
            log.warning(
                "SPACE_ROUTED: unknown inner_event_type=%r from %s; dropping",
                inner_type_raw,
                event.from_instance,
            )
            return

        now = time.monotonic()
        self._prune_expired(now)

        # Loop guard. Applied to the forward leg only — the reply
        # leg's "dedup" is the one-shot ``_origin_eph_state.pop`` on
        # unwrap (any second reply for the same route_id finds no
        # cached priv and is dropped there). Without this carve-out,
        # the origin's own ``send_routed`` entry in ``_seen_routes``
        # would block the reply that's threading back to it.
        if direction == "forward":
            if route_id in self._seen_routes:
                return
            self._seen_routes[route_id] = now + self._seen_ttl_s

        self_id = self._federation.own_instance_id
        next_index = position + 1
        next_hop = path[next_index]
        # Anti-spoof: the previous hop named at ``path[position]`` must be
        # the (§24.11-authenticated) sender. A mismatch means the envelope
        # was mis-routed or someone is replaying it off-path — drop rather
        # than forward. (The seal binds content to the target, so a
        # mismatched relay still couldn't read it; this just stops a stray
        # envelope from being re-fanned down the chain.)
        if path[position] != event.from_instance:
            log.warning(
                "SPACE_ROUTED route_id=%s: path[%d]=%s != from_instance=%s; "
                "dropping (mis-route/spoof)",
                route_id[:8],
                position,
                path[position],
                event.from_instance,
            )
            return
        # Cycle: do we appear in the forward path? (Beyond
        # position+1 — being position+1 is the legitimate "we are
        # the next hop" case.)
        if self_id in path[next_index + 1 :]:
            log.warning(
                "SPACE_ROUTED route_id=%s: cycle — self appears later in path",
                route_id[:8],
            )
            return
        # Defensive: SPACE_ROUTED that arrives at us but path[next]
        # is not us → we're not actually the next hop. Drop instead
        # of forwarding to keep a stray envelope from being re-fanned.
        if next_hop != self_id:
            log.warning(
                "SPACE_ROUTED route_id=%s: wrong-next-hop (path[%s]=%s, self=%s)",
                route_id[:8],
                next_index,
                next_hop,
                self_id,
            )
            return

        # We are at ``next_index``. If that's the last element, unwrap.
        if next_index == len(path) - 1:
            await self._unwrap_and_dispatch(
                event=event,
                route_id=route_id,
                path=path,
                direction=direction,
                inner_type=inner_type,
                inner_type_raw=inner_type_raw,
                sealed=sealed_raw,
                now=now,
            )
            return

        # Otherwise: forward to path[next_index + 1] with position
        # bumped. Relays propagate the sealed blob untouched — only
        # the endpoint that holds the matching ephemeral private half
        # can read the inner payload.
        forward_to = path[next_index + 1]
        try:
            await self._federation.send_event(
                to_instance_id=forward_to,
                event_type=FederationEventType.SPACE_ROUTED,
                payload={
                    "route_id": route_id,
                    "path": list(path),
                    "position": next_index,
                    "direction": direction,
                    "inner_event_type": inner_type_raw,
                    "sealed": sealed_raw,
                },
            )
        except Exception:
            log.warning(
                "SPACE_ROUTED forward to %s failed",
                forward_to,
                exc_info=True,
            )

    # ── Internal: unwrap + dispatch ────────────────────────────────────

    async def _unwrap_and_dispatch(
        self,
        *,
        event: FederationEvent,
        route_id: str,
        path: list[str],
        direction: str,
        inner_type: FederationEventType,
        inner_type_raw: str,
        sealed: dict,
        now: float,
    ) -> None:
        """Decrypt the sealed inner payload and re-dispatch it.

        Forward-leg unwrap (direction="forward"): we are the target.
        Look up our target-side ephemeral priv via the discovery
        service, decrypt the origin→target ciphertext, stash the
        reply state so the inner handler can ship via
        :meth:`send_routed_reply`, and dispatch the synthesised event.

        Reply-leg unwrap (direction="reply"): we are the origin.
        Look up our origin-side ephemeral priv keyed on ``route_id``,
        decrypt the target→origin ciphertext (different key + ack
        AAD) and dispatch.
        """
        self_id = self._federation.own_instance_id
        if direction == "forward":
            target_pub = str(sealed.get("target_eph_pk") or "")
            if not target_pub:
                log.warning(
                    "SPACE_ROUTED route_id=%s: forward sealed missing target_eph_pk",
                    route_id[:8],
                )
                return
            target_priv = self._target_eph_lookup(target_pub)
            if target_priv is None:
                await self._nack_stale_target_eph(
                    event=event,
                    route_id=route_id,
                    path=path,
                    target_pub=target_pub,
                )
                return
            try:
                inner_payload_json = routed_crypto.unseal_inner_payload(
                    sealed=sealed,
                    target_eph_priv_b64=target_priv,
                    route_id=route_id,
                    inner_event_type=inner_type_raw,
                )
            except Exception:
                log.warning(
                    "SPACE_ROUTED route_id=%s: forward unseal failed; dropping",
                    route_id[:8],
                    exc_info=True,
                )
                return
            try:
                inner_payload = json.loads(inner_payload_json)
            except ValueError, TypeError:
                log.warning(
                    "SPACE_ROUTED route_id=%s: inner_payload JSON parse failed",
                    route_id[:8],
                )
                return
            if not isinstance(inner_payload, dict):
                log.warning(
                    "SPACE_ROUTED route_id=%s: decoded inner_payload is not a"
                    " dict; dropping",
                    route_id[:8],
                )
                return
            # Cache reply state — sealed["origin_eph_pk"] tells us the
            # peer we'll derive the reply key against.
            origin_pub = str(sealed.get("origin_eph_pk") or "")
            self._reply_eph_state[route_id] = (
                target_priv,
                target_pub,
                origin_pub,
                list(reversed(path)),
                now + self._eph_ttl_s,
            )
            origin = path[0]
            synth = FederationEvent(
                msg_id=event.msg_id,
                event_type=inner_type,
                from_instance=origin,
                to_instance=self_id,
                timestamp=event.timestamp,
                payload=inner_payload,
                space_id=event.space_id,
                epoch=event.epoch,
                routed_path=list(path),
                routed_route_id=route_id,
            )
            await self._dispatcher(synth)
            return

        # direction == "reply" — we are the origin.
        entry = self._origin_eph_state.pop(route_id, None)
        if entry is None:
            log.warning(
                "SPACE_ROUTED route_id=%s: no cached origin_eph_priv"
                " (expired or unsolicited reply); dropping",
                route_id[:8],
            )
            return
        origin_priv, _origin_pub, _exp = entry
        try:
            inner_payload_json = routed_crypto.unseal_reply_payload(
                sealed=sealed,
                origin_eph_priv_b64=origin_priv,
                route_id=route_id,
                inner_event_type=inner_type_raw,
            )
        except Exception:
            log.warning(
                "SPACE_ROUTED route_id=%s: reply unseal failed; dropping",
                route_id[:8],
                exc_info=True,
            )
            return
        try:
            inner_payload = json.loads(inner_payload_json)
        except ValueError, TypeError:
            log.warning(
                "SPACE_ROUTED route_id=%s: reply inner_payload JSON parse failed",
                route_id[:8],
            )
            return
        if not isinstance(inner_payload, dict):
            log.warning(
                "SPACE_ROUTED route_id=%s: reply inner_payload is not a dict",
                route_id[:8],
            )
            return
        origin = path[0]
        synth = FederationEvent(
            msg_id=event.msg_id,
            event_type=inner_type,
            from_instance=origin,
            to_instance=self_id,
            timestamp=event.timestamp,
            payload=inner_payload,
            space_id=event.space_id,
            epoch=event.epoch,
            # The reply is the terminal leg of the round-trip — no
            # need to set routed_route_id, no further reply expected.
            routed_path=list(path),
        )
        await self._dispatcher(synth)

    # ── Route-stale nack (SPACE_ROUTE_STALE) ───────────────────────────

    async def _nack_stale_target_eph(
        self,
        *,
        event: FederationEvent,
        route_id: str,
        path: list[str],
        target_pub: str,
    ) -> None:
        """Target-side: we hold no private half for ``target_pub`` (we
        restarted, or the origin's route cache outlived our key). Tell
        the previous hop so the nack can walk back to the origin — or,
        if that hop predates v_28, drop exactly as before.

        The destination is ``event.from_instance`` — never recomputed
        from ``path``: :meth:`_on_routed` already enforced
        ``path[position] == event.from_instance`` before calling the
        unwrap, so the two agree, and the §24.11-authenticated sender
        is the value we trust.
        """
        prev_hop = event.from_instance
        if not await self._federation.peer_supports(
            prev_hop, min_version=FederationCapability.MIN_FOR_ROUTE_STALE_NACK
        ):
            log.warning(
                "SPACE_ROUTED route_id=%s: no cached target_eph_priv"
                " for pub=%s (expired or unknown); dropping",
                route_id[:8],
                target_pub[:8],
            )
            log.debug(
                "SPACE_ROUTED route_id=%s: previous hop %s is pre-v_28;"
                " no SPACE_ROUTE_STALE nack sent",
                route_id[:8],
                prev_hop,
            )
            return
        payload = {
            "route_id": route_id,
            "path": list(path),
            # Mirrors SPACE_ROUTED: ``position`` names the hop that SENT
            # this nack hop — the target sits at the end of the path.
            "position": len(path) - 1,
            "target_identity_pk": self._federation.own_identity_pk.hex(),
            "stale_eph_pk": target_pub,
            "sig": routed_crypto.sign_route_stale(
                seed=self._federation.own_identity_seed,
                route_id=route_id,
                stale_eph_pk_b64=target_pub,
            ),
            "sig_suite": routed_crypto.ROUTE_STALE_SIG_SUITE_ED25519,
        }
        try:
            await self._federation.send_event(
                to_instance_id=prev_hop,
                event_type=FederationEventType.SPACE_ROUTE_STALE,
                payload=payload,
            )
        except Exception:
            log.warning(
                "SPACE_ROUTED route_id=%s: SPACE_ROUTE_STALE nack to %s failed",
                route_id[:8],
                prev_hop,
                exc_info=True,
            )
            return
        # A handled condition — INFO, not WARNING: the demo harness's log
        # audit treats unexpected WARNINGs as failures, and the nack IS
        # the recovery path here.
        log.info(
            "SPACE_ROUTED route_id=%s: no cached target_eph_priv for pub=%s"
            " (expired or unknown); nacked to %s",
            route_id[:8],
            target_pub[:8],
            prev_hop,
        )

    async def _on_route_stale(self, event: FederationEvent) -> None:
        """Walk a ``SPACE_ROUTE_STALE`` nack one hop back toward the origin.

        ``position`` names the hop that SENT this nack hop, so a relay
        at index ``i`` receives ``position == i + 1`` and forwards to
        ``path[i - 1]`` with ``position = i``. When ``position - 1 == 0``
        we are the origin and hand off to
        :meth:`_on_route_stale_at_origin`.

        Structural validation mirrors :meth:`_on_routed`. Relays do NOT
        verify the Ed25519 signature: verifying needs nothing a relay
        has that the origin lacks (the identity pk travels in the
        payload and is pinned by ``derive_instance_id`` against
        ``path[-1]``), the same bytes reach the origin either way, and
        a relay that verified would be deciding for the origin which
        nacks it gets to see. The origin (T3) is the enforcement point.
        Fail-soft throughout — a nack never raises into the dispatcher.
        """
        p = event.payload
        route_id_raw = p.get("route_id")
        path_raw = p.get("path")
        position_raw = p.get("position")
        sig = p.get("sig")
        sig_suite = p.get("sig_suite")
        target_identity_pk = p.get("target_identity_pk")
        stale_eph_pk = p.get("stale_eph_pk")
        if (
            not isinstance(route_id_raw, str)
            or not route_id_raw
            or not isinstance(path_raw, list)
            or not path_raw
            or not all(isinstance(h, str) for h in path_raw)
            or isinstance(position_raw, bool)
            or not isinstance(position_raw, int)
            or not isinstance(sig, str)
            or not isinstance(sig_suite, str)
            or not isinstance(target_identity_pk, str)
            or not isinstance(stale_eph_pk, str)
        ):
            log.debug(
                "SPACE_ROUTE_STALE: malformed payload from %s; dropping",
                event.from_instance,
            )
            return
        route_id: str = route_id_raw
        path: list[str] = list(path_raw)
        position: int = position_raw
        if len(path) > _MAX_ROUTED_PATH_LEN:
            log.warning(
                "SPACE_ROUTE_STALE route_id=%s: path too long (%d > %d); dropping",
                route_id[:8],
                len(path),
                _MAX_ROUTED_PATH_LEN,
            )
            return
        if not 1 <= position < len(path):
            log.debug(
                "SPACE_ROUTE_STALE route_id=%s: position=%s out of range for"
                " path_len=%s; dropping",
                route_id[:8],
                position,
                len(path),
            )
            return
        # Anti-spoof: the hop named at ``path[position]`` must be the
        # §24.11-authenticated sender.
        if path[position] != event.from_instance:
            log.warning(
                "SPACE_ROUTE_STALE route_id=%s: path[%d]=%s != from_instance=%s;"
                " dropping (mis-route/spoof)",
                route_id[:8],
                position,
                path[position],
                event.from_instance,
            )
            return
        self_id = self._federation.own_instance_id
        if path[position - 1] != self_id:
            log.warning(
                "SPACE_ROUTE_STALE route_id=%s: wrong-next-hop (path[%d]=%s, self=%s)",
                route_id[:8],
                position - 1,
                path[position - 1],
                self_id,
            )
            return
        # Cheap early-shed, no key material needed: the carried identity
        # pk must be the one ``path[-1]`` is derived from, or this cannot
        # be a nack for this route. Bad hex / wrong length → ValueError.
        try:
            derived = derive_instance_id(bytes.fromhex(target_identity_pk))
        except ValueError:
            log.warning(
                "SPACE_ROUTE_STALE route_id=%s: malformed target_identity_pk; dropping",
                route_id[:8],
            )
            return
        if derived != path[-1]:
            log.warning(
                "SPACE_ROUTE_STALE route_id=%s: target_identity_pk derives to %s,"
                " not path[-1]=%s; dropping",
                route_id[:8],
                derived,
                path[-1],
            )
            return
        # Dedup AFTER every structural check, on purpose. Everything
        # above is a deterministic reject of input the legitimate
        # previous hop would never send, so no genuine nack is lost by
        # checking it first — and only that authenticated previous hop
        # (``path[position] == from_instance``, ``path[position-1] ==
        # self``) can reach this line. Dedup-first would let any peer
        # that learned the route_id (a relay further down the path,
        # say, which is also paired with us) burn the slot with a
        # malformed nack and suppress the real one that follows.
        now = time.monotonic()
        self._prune_expired(now)
        if route_id in self._seen_nacks:
            log.debug(
                "SPACE_ROUTE_STALE route_id=%s: already forwarded; ignoring duplicate",
                route_id[:8],
            )
            return
        self._seen_nacks[route_id] = now + self._seen_ttl_s

        if position - 1 == 0:
            await self._on_route_stale_at_origin(
                event=event,
                route_id=route_id,
                path=path,
                target_identity_pk=target_identity_pk,
                stale_eph_pk=stale_eph_pk,
                sig=sig,
                sig_suite=sig_suite,
            )
            return

        next_hop = path[position - 2]
        if not await self._federation.peer_supports(
            next_hop, min_version=FederationCapability.MIN_FOR_ROUTE_STALE_NACK
        ):
            # Degrades to today's behaviour for that leg: the origin
            # never learns, and re-seals until its route cache expires.
            log.debug(
                "SPACE_ROUTE_STALE route_id=%s: next hop %s is pre-v_28;"
                " nack ends here",
                route_id[:8],
                next_hop,
            )
            return
        try:
            await self._federation.send_event(
                to_instance_id=next_hop,
                event_type=FederationEventType.SPACE_ROUTE_STALE,
                payload={
                    "route_id": route_id,
                    "path": list(path),
                    "position": position - 1,
                    "target_identity_pk": target_identity_pk,
                    "stale_eph_pk": stale_eph_pk,
                    "sig": sig,
                    "sig_suite": sig_suite,
                },
            )
        except Exception:
            log.warning(
                "SPACE_ROUTE_STALE route_id=%s: forward to %s failed",
                route_id[:8],
                next_hop,
                exc_info=True,
            )

    async def _on_route_stale_at_origin(
        self,
        *,
        event: FederationEvent,
        route_id: str,
        path: list[str],
        target_identity_pk: str,
        stale_eph_pk: str,
        sig: str,
        sig_suite: str,
    ) -> None:
        """Origin-side handling of a ``SPACE_ROUTE_STALE`` nack — the
        enforcement point.

        :meth:`_on_route_stale` has already checked the structural
        invariants (path cap, position, ``path[position] == sender``,
        ``path[0] == self``, ``derive_instance_id(pk) == path[-1]``,
        hop-level dedup). This method does the origin-only work, in
        order, each step a fail-closed drop:

        1. ``route_id`` names a live :class:`_PendingRouted` — bounds
           replay to the ephemeral window; a nack for a send we never
           made is a no-op.
        2. ``path[-1]`` is that record's target.
        3. ``target_identity_pk`` equals the pk *we* pinned at discovery
           (second, independent binding — the relay check only proved
           the carried pk derives to ``path[-1]``). An empty pin (legacy
           caller) falls back to re-deriving against the record's target.
        4. The Ed25519 signature verifies; an unknown ``sig_suite`` is a
           hard reject, never a fallback.
        5. Pop the record — one nack per envelope.
        6. Invalidate the cached route.
        7. Retransmit the retained inner once (never for a nacked retry,
           never for an oversized inner, never without a fresh route).

        Drops of expected attacker / replay input log at DEBUG or INFO;
        WARNING is reserved for a send failure. Never raises.
        """
        now = time.monotonic()
        pending = self._pending_routed.get(route_id)
        if pending is None or pending.expires_at <= now:
            log.debug(
                "SPACE_ROUTE_STALE route_id=%s from %s: no pending routed send for"
                " this route_id (not ours / too old); ignoring",
                route_id[:8],
                event.from_instance,
            )
            return
        target = pending.target_instance_id
        if path[-1] != target:
            log.debug(
                "SPACE_ROUTE_STALE route_id=%s: names a different target"
                " (path[-1]=%s, we sent to %s); ignoring",
                route_id[:8],
                path[-1],
                target,
            )
            return
        if pending.target_identity_pk:
            if target_identity_pk != pending.target_identity_pk:
                log.debug(
                    "SPACE_ROUTE_STALE route_id=%s: target_identity_pk differs from"
                    " the key pinned at discovery for %s; ignoring",
                    route_id[:8],
                    target,
                )
                return
        else:
            # No pin available for this send (a caller predating the
            # pin): the derive binding to OUR recorded target is the
            # only key binding left — re-check it here rather than lean
            # on the relay step alone. Never acceptance on its own; the
            # signature check below still has to pass.
            log.debug(
                "SPACE_ROUTE_STALE route_id=%s: identity pin unavailable for"
                " this send; relying on derive_instance_id + signature",
                route_id[:8],
            )
            try:
                derived = derive_instance_id(bytes.fromhex(target_identity_pk))
            except ValueError:
                log.debug(
                    "SPACE_ROUTE_STALE route_id=%s: malformed target_identity_pk;"
                    " ignoring",
                    route_id[:8],
                )
                return
            if derived != target:
                log.debug(
                    "SPACE_ROUTE_STALE route_id=%s: target_identity_pk derives to"
                    " %s, not our target %s; ignoring",
                    route_id[:8],
                    derived,
                    target,
                )
                return
        try:
            verified = routed_crypto.verify_route_stale(
                identity_pk=bytes.fromhex(target_identity_pk),
                route_id=route_id,
                stale_eph_pk_b64=stale_eph_pk,
                sig_b64=sig,
                sig_suite=sig_suite,
            )
        except routed_crypto.UnsupportedRouteStaleSuite:
            log.info(
                "SPACE_ROUTE_STALE route_id=%s: unsupported sig_suite=%r;"
                " ignoring (no fallback)",
                route_id[:8],
                sig_suite,
            )
            return
        except ValueError:
            log.debug(
                "SPACE_ROUTE_STALE route_id=%s: malformed target_identity_pk; ignoring",
                route_id[:8],
            )
            return
        if not verified:
            log.info(
                "SPACE_ROUTE_STALE route_id=%s: signature does not verify under the"
                " target identity for %s; ignoring",
                route_id[:8],
                target,
            )
            return
        # Verified. One nack per envelope: consume the record before any
        # await so a concurrent duplicate finds nothing.
        self._pending_routed.pop(route_id, None)
        route_service = self._route_service
        if route_service is None:
            log.info(
                "SPACE_ROUTE_STALE route_id=%s: verified for %s but no route"
                " service attached; route not invalidated",
                route_id[:8],
                target,
            )
            return
        try:
            await route_service.invalidate(target)
            if pending.is_retry:
                log.info(
                    "SPACE_ROUTE_STALE route_id=%s: route to %s invalidated; retry"
                    " also nacked, giving up on %s",
                    route_id[:8],
                    target,
                    pending.inner_event_type.value,
                )
                return
            if pending.inner_payload is None:
                log.info(
                    "SPACE_ROUTE_STALE route_id=%s: route to %s invalidated;"
                    " oversized inner %s not retransmitted (durable outbox will"
                    " retry)",
                    route_id[:8],
                    target,
                    pending.inner_event_type.value,
                )
                return
            # ``discover_route`` single-flights per target and honours the
            # negative cooldown, so a burst of nacks for one target costs
            # one flood.
            discovery = await route_service.discover_route(target)
            if discovery is None or len(discovery[0]) < 2:
                log.info(
                    "SPACE_ROUTE_STALE route_id=%s: route to %s invalidated; no"
                    " route on rediscovery, giving up on %s",
                    route_id[:8],
                    target,
                    pending.inner_event_type.value,
                )
                return
            new_path, new_eph = discovery
            new_route_id = await self.send_routed(
                path=new_path,
                target_eph_pk_b64=new_eph,
                inner_event_type=pending.inner_event_type,
                inner_payload=pending.inner_payload,
                target_identity_pk=route_service.cached_target_identity_pk(target)
                or "",
                is_retry=True,
            )
        except Exception:
            log.warning(
                "SPACE_ROUTE_STALE route_id=%s: rediscover/retransmit to %s failed",
                route_id[:8],
                target,
                exc_info=True,
            )
            return
        log.info(
            "SPACE_ROUTE_STALE route_id=%s: route to %s invalidated, rediscovered,"
            " retransmitted %s as route_id=%s",
            route_id[:8],
            target,
            pending.inner_event_type.value,
            new_route_id[:8],
        )

    # ── Helpers ────────────────────────────────────────────────────────

    def _prune_expired(self, now: float) -> None:
        if self._seen_routes:
            self._seen_routes = cap_by_expiry(
                {k: v for k, v in self._seen_routes.items() if v > now},
                key=lambda kv: kv[1],
            )
        if self._seen_nacks:
            self._seen_nacks = cap_by_expiry(
                {k: v for k, v in self._seen_nacks.items() if v > now},
                key=lambda kv: kv[1],
            )
        self._prune_eph_state(now)

    def _prune_eph_state(self, now: float) -> None:
        # Same defense-in-depth cap as the discovery service: TTL
        # prune handles steady-state, ``cap_by_expiry`` is the
        # ceiling against an attacker pumping unique route_ids faster
        # than the TTL window.
        if self._origin_eph_state:
            self._origin_eph_state = cap_by_expiry(
                {k: v for k, v in self._origin_eph_state.items() if v[2] > now},
                key=lambda kv: kv[1][2],
            )
        if self._reply_eph_state:
            self._reply_eph_state = cap_by_expiry(
                {k: v for k, v in self._reply_eph_state.items() if v[4] > now},
                key=lambda kv: kv[1][4],
            )
        if self._pending_routed:
            self._pending_routed = cap_by_expiry(
                {k: v for k, v in self._pending_routed.items() if v.expires_at > now},
                key=lambda kv: kv[1].expires_at,
                cap=_MAX_PENDING_ROUTED,
            )

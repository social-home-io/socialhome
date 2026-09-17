"""Federation-mesh route discovery (§D2 PR 2 — v_6).

In-memory BFS over the federation graph: when a local user wants to
reach an instance they are *not* directly paired with, the origin
floods a :data:`FederationEventType.SPACE_FIND_ROUTE` probe out to
each of its confirmed peers, each of which forwards the probe to its
own peers, bounded by ``max_hops``. A peer that finds the target
locally (either the target *is* them, or it's one of their confirmed
peers) ships a :data:`FederationEventType.SPACE_ROUTE_FOUND` back
along the same chain via cached ``{request_id: caller_instance_id}``
entries. The origin collects responses for a brief window and picks
the shortest path (random tie-break among equally-short candidates).

The result is cached for ``cache_ttl_s`` so subsequent
``discover_route(target)`` calls on the same target hit the cache —
typical for a session: paste an invite code, the discovery runs
once, then every subsequent envelope (REDEEM → ACK → future
multi-hop space content) re-uses the path.

This mirrors :class:`AutoPairCoordinator` in shape — relay forwarding
with caller-side cache + nonce dedup — but the unit of dispatch is a
path through the graph, not a pair-introduction.

Sub-v_6 peers do not understand ``SPACE_FIND_ROUTE`` /
``SPACE_ROUTED`` and would 400 the envelope, so the relay step gates
each candidate peer on ``peer_supports(min_version=6)`` before
forwarding the probe — older peers are invisible to the mesh.

Privacy invariant: route discovery leaks only ``instance_id`` values
that the origin is already willing to talk to (its confirmed peers
already see its envelopes). Relay hops see the origin's instance_id
(they signed the inbound wrapper anyway) and the target's
instance_id (the user just decided to reach out). No user, post, or
content metadata appears in discovery payloads.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    sign_ed25519,
    verify_ed25519,
)
from ..domain.federation import FederationEventType, PairingStatus
from ..domain.federation_capabilities import FederationCapability
from . import routed_crypto

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent, RemoteInstance
    from ..repositories.federation_repo import AbstractFederationRepo
    from .federation_service import FederationService

log = logging.getLogger(__name__)


#: Safety margin subtracted from :data:`routed_crypto.DEFAULT_TARGET_EPH_TTL_S`
#: to get the origin-side route-cache TTL. The origin seals every routed
#: payload under the ``target_eph_pk`` this cache holds, but the matching
#: private half lives in the *target's* memory under its own TTL — so the
#: origin's window MUST close first, or the origin keeps sealing under a key
#: the target has already dropped and every envelope is silently discarded on
#: arrival (#648). The margin covers one send's duration plus clock drift
#: between the two households.
ROUTE_CACHE_SAFETY_MARGIN_S: float = 30.0

#: Origin-side route-cache TTL. Derived, never hand-tuned, so the invariant
#: ``route cache TTL < target ephemeral TTL`` cannot drift apart in a later
#: edit. Pinned by ``tests/federation/test_route_discovery.py``.
ROUTE_CACHE_TTL_S: float = (
    routed_crypto.DEFAULT_TARGET_EPH_TTL_S - ROUTE_CACHE_SAFETY_MARGIN_S
)

#: How long a target stays in the negative-result cooldown after a discovery
#: found no route. Without it, a caller in a per-chunk retry loop re-floods
#: every mesh-capable peer for each chunk.
ROUTE_NEGATIVE_COOLDOWN_S: float = 30.0


#: Required peer ``proto_version`` for participating in mesh routing.
#: Anything below v_6 doesn't know SPACE_FIND_ROUTE / SPACE_ROUTED.
_MIN_MESH_PROTO_VERSION = FederationCapability.MIN_FOR_SPACE_INVITE_REDEEM

#: Hard cap on each in-memory state dict (``_seen_requests``,
#: ``_caller_cache``, ``_route_cache``, ``_target_eph_state``). The
#: TTL prune handles steady-state cleanup; this cap is the
#: defense-in-depth ceiling so a hostile authenticated peer that
#: pumps unique ``request_id`` / ``target_eph_pk`` values faster than
#: the TTL window can't push us into unbounded memory growth. At ~150
#: bytes per entry the cap costs ~750 KiB per dict in the worst case
#: — small enough to swallow, large enough that a real federation
#: graph won't hit it during normal operation. The §24.11 signature +
#: replay pipeline already bounds attack input to *confirmed* peers,
#: so the cap is the floor of a layered defence rather than the only
#: line.
_MAX_CACHE_ENTRIES: int = 5000


def _route_found_signing_bytes(request_id: str, target_eph_pk_b64: str) -> bytes:
    """Canonical bytes the target signs to bind its ephemeral X25519 pub
    to THIS discovery request.

    Domain-separated (``space-route-found:v1:``) and request-scoped so a
    signature can't be lifted onto another ``request_id`` (replay) and
    can't collide with any other Ed25519 signature this identity
    produces. See :meth:`RouteDiscoveryService._on_route_found` (origin
    branch) for the verification that closes the relay-MITM where a
    confirmed peer substitutes its own ephemeral key (§D2).
    """
    return (
        b"space-route-found:v1:"
        + request_id.encode()
        + b":"
        + target_eph_pk_b64.encode()
    )


def cap_by_expiry(
    items: dict,
    *,
    key: Callable[[tuple[Any, Any]], float],
    cap: int = _MAX_CACHE_ENTRIES,
) -> dict:
    """Return ``items`` truncated to at most ``cap`` entries, keeping
    the ones with the latest expiry. Used by the prune step in both
    :class:`RouteDiscoveryService` and
    :class:`socialhome.federation.routed_envelope.SpaceRoutedHandler`
    to cap each in-memory state dict — entries that haven't expired
    yet but would push us over the cap are evicted oldest-first.

    Package-public (no leading underscore) so ``routed_envelope`` can
    re-use the same eviction policy without duplicating the logic.
    """
    if len(items) <= cap:
        return items
    sorted_items = sorted(items.items(), key=key, reverse=True)
    return dict(sorted_items[:cap])


@dataclass(slots=True)
class _PendingDiscovery:
    """Origin-side bookkeeping for an in-flight discovery probe.

    Collects every ROUTE_FOUND (path + target ephemeral pub) that
    lands for ``request_id`` and resolves ``future`` after
    ``discovery_timeout_s`` from the first response — that way a
    single fast hop doesn't pay the full timeout window.
    """

    future: asyncio.Future[tuple[list[str], str] | None]
    target: str
    #: Each response is ``(path, target_eph_pk_b64)``. The target
    #: ephemeral pub is required to seal the inner payload before
    #: the first SPACE_ROUTED hits the wire — see
    #: :mod:`socialhome.federation.routed_crypto`.
    responses: list[tuple[list[str], str]] = field(default_factory=list)
    # ``resolved`` flips True once the resolver task fires so a late
    # ROUTE_FOUND for an already-decided request is a no-op.
    resolved: bool = False


@dataclass(slots=True)
class _CachedRoute:
    path: list[str]
    #: Target's ephemeral X25519 pub, base64url-encoded. Cached so a
    #: re-discovery within the route TTL doesn't trigger a fresh probe
    #: just to refresh the encryption key — origin can re-seal under
    #: the same target pub for the duration of the cache window.
    target_eph_pk: str
    expires_at: float


class RouteDiscoveryService:
    """BFS-flooded route discovery service.

    See module docstring for the protocol shape. Construction is
    pure — no I/O. Wire the inbound handlers via :meth:`attach_to`
    once a :class:`FederationService` exists.
    """

    __slots__ = (
        "_federation",
        "_federation_repo",
        "_max_hops",
        "_cache_ttl_s",
        "_seen_ttl_s",
        "_discovery_timeout_s",
        "_target_eph_ttl_s",
        "_pending",
        "_seen_requests",
        "_caller_cache",
        "_route_cache",
        "_target_eph_state",
        "_inflight",
        "_negative_until",
        "_origin_requests",
    )

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        max_hops: int = 3,
        cache_ttl_s: float = ROUTE_CACHE_TTL_S,
        seen_ttl_s: float = 60.0,
        discovery_timeout_s: float = 2.0,
    ) -> None:
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._max_hops = max_hops
        self._cache_ttl_s = cache_ttl_s
        self._seen_ttl_s = seen_ttl_s
        self._discovery_timeout_s = discovery_timeout_s
        #: TTL on cached *target-side* ephemeral private halves. The
        #: target mints a fresh keypair per FIND_ROUTE it answers and
        #: keeps the priv around for this long so the inbound
        #: SPACE_ROUTED can be unsealed. Deliberately LONGER than the
        #: origin-side route-cache TTL by
        #: :data:`ROUTE_CACHE_SAFETY_MARGIN_S` — an earlier revision of
        #: this comment claimed the two "match", which was both untrue
        #: (the origin stamped its window at resolve time, strictly
        #: after the target minted) and the wrong shape: if the origin's
        #: window can outlive the target's, the origin seals under a
        #: dead key and the target drops the envelope in silence (#648).
        self._target_eph_ttl_s = routed_crypto.DEFAULT_TARGET_EPH_TTL_S

        #: ``request_id`` → in-flight bookkeeping (origin side).
        self._pending: dict[str, _PendingDiscovery] = {}
        #: ``request_id`` → wall-clock expiry. Drops repeats while we
        #: still expect more forwards to arrive from neighbour peers.
        self._seen_requests: dict[str, float] = {}
        #: ``request_id`` → caller's instance_id (relay side).
        #: Lets a relay route the ROUTE_FOUND response back along the
        #: same chain the FIND_ROUTE traversed.
        self._caller_cache: dict[str, tuple[str, float]] = {}
        #: target_instance_id → in-flight discovery. Without this a
        #: caller in a per-chunk loop (the §25.6 catch-up stream) floods
        #: every mesh-capable peer once per chunk; with it, concurrent
        #: callers for the same target share one probe.
        self._inflight: dict[str, asyncio.Future] = {}
        #: target_instance_id → monotonic deadline before which we won't
        #: re-probe after a discovery that found no route.
        self._negative_until: dict[str, float] = {}
        #: ``request_id`` → ``(target, expires_at)`` for probes WE
        #: originated. Outlives ``_pending`` (which is popped the moment
        #: the wait resolves) so a ROUTE_FOUND that arrives after the hard
        #: cap can still be attributed to its target and warmed into the
        #: route cache instead of being thrown away — see
        #: :meth:`_on_route_found`.
        self._origin_requests: dict[str, tuple[str, float]] = {}
        #: target_instance_id → cached path (origin side).
        self._route_cache: dict[str, _CachedRoute] = {}
        #: target-eph-pub-b64 → ``(target_eph_priv_b64, expires_at)``.
        #: Populated on this instance when a SPACE_FIND_ROUTE targets
        #: us — we mint a fresh keypair, ship the pub via
        #: SPACE_ROUTE_FOUND, and cache the priv so the inbound
        #: SPACE_ROUTED carrying the matching pub can be unsealed.
        self._target_eph_state: dict[str, tuple[str, float]] = {}

    # ── Attach ─────────────────────────────────────────────────────────

    def attach_to(self, federation_service: "FederationService") -> None:
        """Wire the two inbound event-type handlers into the registry."""
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_FIND_ROUTE,
            self._on_find_route,
        )
        registry.register(
            FederationEventType.SPACE_ROUTE_FOUND,
            self._on_route_found,
        )

    # ── Public API ─────────────────────────────────────────────────────

    async def discover_route(
        self,
        target_instance_id: str,
    ) -> tuple[list[str], str] | None:
        """Return ``(path, target_eph_pk_b64)`` for ``target_instance_id``.

        ``path = [self_id, hop_1, ..., target_id]`` on success, or
        ``None`` if no route is found within ``discovery_timeout_s``.
        The second tuple element is the target's ephemeral X25519
        public key (b64url) the caller passes into
        :meth:`SpaceRoutedHandler.send_routed` so the inner payload
        is sealed for the target. Caches the result for
        ``cache_ttl_s``; subsequent calls hit the cache.

        Local short-circuit: if ``target_instance_id`` is the local
        instance, mints a target ephemeral synchronously and returns
        ``([self], pub_b64)`` — no probe.

        Direct-peer short-circuit is deliberately NOT used because a
        direct-peer return path can't produce a ``target_eph_pk``
        without round-tripping at least one probe; we fall through to
        the BFS path which resolves in a single hop against a
        confirmed mesh-capable peer.
        """
        now = time.monotonic()
        self._prune_expired(now)

        # 1) Cache hit
        cached = self._route_cache.get(target_instance_id)
        if cached is not None and cached.expires_at > now:
            return list(cached.path), cached.target_eph_pk

        self_id = self._federation.own_instance_id

        # 2) Local — we *are* the target. Mint our own ephemeral on
        # the fly + cache the priv so the inbound SPACE_ROUTED that
        # the caller is about to ship can be unsealed.
        if target_instance_id == self_id:
            target_eph_pk_b64 = self._generate_target_eph(now)
            local_path = [self_id]
            self._route_cache[target_instance_id] = _CachedRoute(
                path=local_path,
                target_eph_pk=target_eph_pk_b64,
                expires_at=now + self._cache_ttl_s,
            )
            return list(local_path), target_eph_pk_b64

        # 3) Negative-result cooldown — a target we just failed to
        # reach doesn't get re-flooded on the very next chunk.
        cooldown_until = self._negative_until.get(target_instance_id)
        if cooldown_until is not None and cooldown_until > now:
            return None

        # 4) Single-flight. Concurrent callers for the same target share
        # one flood instead of each spraying every mesh-capable peer.
        # ``shield`` so one caller's cancellation doesn't kill the probe
        # the others are still waiting on.
        inflight = self._inflight.get(target_instance_id)
        if inflight is not None and not inflight.done():
            return await asyncio.shield(inflight)

        fut: asyncio.Future[tuple[list[str], str] | None]
        fut = asyncio.get_event_loop().create_future()
        self._inflight[target_instance_id] = fut
        try:
            result = await self._flood_discover(
                target_instance_id=target_instance_id,
                now=now,
                self_id=self_id,
            )
        except BaseException:
            # Hand waiters a plain "no route" rather than the exception —
            # re-raising into every shielded waiter would also leave an
            # unretrieved-exception warning on the future nobody awaits.
            if not fut.done():
                fut.set_result(None)
            raise
        else:
            if not fut.done():
                fut.set_result(result)
            return result
        finally:
            self._inflight.pop(target_instance_id, None)

    async def _flood_discover(
        self,
        *,
        target_instance_id: str,
        now: float,
        self_id: str,
    ) -> tuple[list[str], str] | None:
        """BFS-flood for ``target_instance_id`` and cache the winner.

        Split out of :meth:`discover_route` so the latter can single-flight
        it; ``now`` is the caller's probe-start timestamp, which is what the
        cache window is anchored on (see :data:`ROUTE_CACHE_SAFETY_MARGIN_S`).
        """
        # Flood probes to each mesh-capable confirmed peer. (Note:
        # there is no direct-peer short-circuit anymore — the BFS
        # resolves in one hop against a direct peer and produces the
        # target's ephemeral pub the seal needs.)
        confirmed = await self._mesh_capable_peers(exclude=set())
        if not confirmed:
            return None

        request_id = secrets.token_hex(16)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[tuple[list[str], str] | None] = loop.create_future()
        self._pending[request_id] = _PendingDiscovery(
            future=fut,
            target=target_instance_id,
        )
        # Origin side: mark the request_id as already-seen on our
        # own instance so a probe that loops back to us (via a peer
        # that has us in its neighbour list) is dropped silently.
        self._seen_requests[request_id] = now + self._seen_ttl_s
        # Remember what we asked for, for longer than we're willing to
        # wait. A slow first round trip (a peer that just rebooted pays a
        # DTLS handshake + STUN) routinely overshoots the hard cap below,
        # and the answer is still perfectly good when it lands.
        self._origin_requests[request_id] = (
            target_instance_id,
            now + self._seen_ttl_s,
        )

        sent = 0
        for peer_inst in confirmed:
            try:
                await self._federation.send_event(
                    to_instance_id=peer_inst.id,
                    event_type=FederationEventType.SPACE_FIND_ROUTE,
                    payload={
                        "request_id": request_id,
                        "target_instance_id": target_instance_id,
                        "hops_traversed": [self_id],
                        "max_hops": self._max_hops,
                        "origin_instance_id": self_id,
                    },
                )
                sent += 1
            except Exception:
                log.warning(
                    "route_discovery: FIND_ROUTE ship to %s failed (continuing)",
                    peer_inst.id,
                    exc_info=True,
                )
        if sent == 0:
            # No peer accepted the probe at all — clean up and bail.
            self._pending.pop(request_id, None)
            return None

        # Hard upper bound on the discovery wait. The collection
        # window started by ``_resolve_after_window`` (on the first
        # response) is also ``discovery_timeout_s``, so the worst
        # case is "first response arrives just before the hard cap
        # fires" — bounded at ``2 * discovery_timeout_s``. When zero
        # responses come back (hop budget exhausted, every peer
        # unreachable), the hard cap is the only thing that resolves
        # the wait.
        result: tuple[list[str], str] | None
        try:
            result = await asyncio.wait_for(
                fut,
                timeout=self._discovery_timeout_s * 2,
            )
        except asyncio.TimeoutError, asyncio.CancelledError:
            result = None
        finally:
            # Mark the pending entry as resolved so a late ROUTE_FOUND
            # doesn't trip a "set_result on done future" assertion.
            pending = self._pending.pop(request_id, None)
            if pending is not None:
                pending.resolved = True

        if result is None:
            self._negative_until[target_instance_id] = (
                time.monotonic() + ROUTE_NEGATIVE_COOLDOWN_S
            )
            return None
        path, target_eph_pk_b64 = result
        # Anchored on the moment the probe was SENT, not on resolution. The
        # target minted its ephemeral strictly after our probe left, so
        # probe-start anchoring removes the discovery-latency term exactly
        # and keeps our window inside the target's (#648).
        self._cache_route(
            target_instance_id=target_instance_id,
            path=path,
            target_eph_pk=target_eph_pk_b64,
            anchored_at=now,
        )
        return result

    def _cache_route(
        self,
        *,
        target_instance_id: str,
        path: list[str],
        target_eph_pk: str,
        anchored_at: float,
    ) -> None:
        """Store a discovered route, anchored on ``anchored_at``.

        ``anchored_at`` must never be later than the moment the target
        minted the ephemeral, or our window could outlive its private half
        (#648). Probe-send time satisfies that for a normal resolve;
        arrival time satisfies it for a late answer (strictly after mint,
        so the window only shrinks).
        """
        self._route_cache[target_instance_id] = _CachedRoute(
            path=list(path),
            target_eph_pk=target_eph_pk,
            expires_at=anchored_at + self._cache_ttl_s,
        )
        self._negative_until.pop(target_instance_id, None)

    def cooldown_remaining(self, target_instance_id: str) -> float:
        """Seconds left on the negative cooldown for ``target_instance_id``.

        ``0.0`` when no cooldown is armed (or it has already elapsed), so a
        truthy value means "the next :meth:`discover_route` will return
        ``None`` WITHOUT probing". Callers use this to tell a transient
        anti-flood window apart from a genuine "we looked and found nothing",
        and to size the wait before retrying — see
        :meth:`FederationService.send_with_mesh_fallback`.

        Pure read: it never arms, extends, or clears the cooldown, so the
        anti-flood property is unaffected by anyone asking.
        """
        until = self._negative_until.get(target_instance_id)
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    async def invalidate(self, target_instance_id: str) -> None:
        """Drop the cached route for ``target_instance_id``.

        Next call to :meth:`discover_route` will run a fresh probe.
        Used when a previously-discovered path stops working (e.g. a
        SPACE_ROUTED envelope to ``path[1]`` returned an unreachable
        signal), so we don't keep retrying a dead chain.
        """
        self._route_cache.pop(target_instance_id, None)
        # Also lift the negative cooldown: ``invalidate`` is an explicit
        # "this path is known-bad, go look again" signal, and leaving the
        # cooldown in place would swallow the very next probe.
        self._negative_until.pop(target_instance_id, None)

    async def invalidate_if_older_than(
        self,
        target_instance_id: str,
        *,
        cutoff: float,
    ) -> bool:
        """Drop the cached route only if it was minted before ``cutoff``.

        A blanket :meth:`invalidate` on every inbound BEGIN is wrong once a
        requester retries: the first BEGIN forces a re-probe, a late
        ROUTE_FOUND warms the cache moments later, and the *next* BEGIN
        would throw that fresh route away and re-probe into the same
        timeout — forever. The requester restarts once, so only a route
        minted before its previous BEGIN can be stale.

        Returns True when the entry was dropped. Monotonic clock, same
        basis as ``_CachedRoute.expires_at``.
        """
        cached = self._route_cache.get(target_instance_id)
        if cached is None:
            return False
        minted_at = cached.expires_at - self._cache_ttl_s
        if minted_at >= cutoff:
            # Minted after the cutoff, so it came from a probe this
            # requester's current process answered. Keep it.
            return False
        self._route_cache.pop(target_instance_id, None)
        self._negative_until.pop(target_instance_id, None)
        return True

    def lookup_target_eph_priv(self, pub_b64: str) -> str | None:
        """Return the cached target-ephemeral *private* half for ``pub_b64``.

        Looked up by :class:`SpaceRoutedHandler` on unwrap: it pulls
        ``sealed["target_eph_pk"]`` from the inbound envelope and
        asks the discovery service for the matching private half so
        ``routed_crypto.unseal_inner_payload`` can decrypt. Returns
        ``None`` if the pub was never seen here OR its TTL has
        elapsed — caller drops the envelope in that case.
        """
        entry = self._target_eph_state.get(pub_b64)
        if entry is None:
            return None
        priv_b64, expires_at = entry
        if time.monotonic() >= expires_at:
            # Lazily reap on lookup. The full prune sweep runs on the
            # next ``_prune_expired`` tick.
            self._target_eph_state.pop(pub_b64, None)
            return None
        return priv_b64

    # ── Inbound handlers ───────────────────────────────────────────────

    async def _on_find_route(self, event: "FederationEvent") -> None:
        """Relay-side: forward / respond to a discovery probe.

        Rules (in order):

        1. Drop if ``request_id`` seen recently (loop prevention).
        2. Drop if self in ``hops_traversed`` (cycle).
        3. Cache ``{request_id: event.from_instance}`` for response
           routing (TTL ``seen_ttl_s``).
        4. If ``target == self``: mint a fresh target ephemeral
           keypair, cache the priv, and respond with
           ``ROUTE_FOUND(path=hops_traversed + [self],
           target_eph_pk=pub)``.
        5. Else if ``len(hops_traversed) < max_hops``: forward
           FIND_ROUTE to each confirmed mesh-capable peer not in
           ``hops_traversed``, with ``hops_traversed + [self]``. (The
           target, when it's a confirmed peer of ours, will be in
           that set and will answer in step 4 on its own side.)
        6. Else silent drop (budget exhausted).

        There is no "respond on behalf of the target" shortcut — only
        the target itself can mint the ephemeral half ``send_routed``
        needs to seal against, so the probe must reach it.
        """
        p = event.payload
        request_id = str(p.get("request_id") or "")
        target = str(p.get("target_instance_id") or "")
        hops_raw = p.get("hops_traversed")
        hops = [str(h) for h in hops_raw] if isinstance(hops_raw, list) else []
        # Caller-supplied max_hops bounds the probe's reach for this
        # particular request; we additionally cap it at our local
        # ``self._max_hops`` so a peer can't burn our outbound budget
        # by sending an inflated bound.
        try:
            caller_max_hops = int(p.get("max_hops") or self._max_hops)
        except TypeError, ValueError:
            caller_max_hops = self._max_hops
        max_hops = min(caller_max_hops, self._max_hops)
        if not request_id or not target:
            log.debug(
                "SPACE_FIND_ROUTE from %s: missing request_id/target",
                event.from_instance,
            )
            return

        now = time.monotonic()
        self._prune_expired(now)
        self_id = self._federation.own_instance_id

        # 2) Cycle: this probe already passed through us.
        if self_id in hops:
            return

        new_hops = [*hops, self_id]

        # 4) Local match — we *are* the target. Mint our ephemeral.
        # NOTE: case 4 deliberately runs *before* the loop-prevention
        # check below. The target needs to respond to every distinct
        # incoming probe (from b's branch AND c's branch in a diamond
        # graph), each with a freshly-minted ephemeral, so the origin
        # has two valid options to tie-break over. Relays still dedup
        # via the same ``_seen_requests`` entry, so the flood doesn't
        # amplify — only the target answers more than once.
        if target == self_id:
            target_eph_pk_b64 = self._generate_target_eph(now)
            await self._send_route_found(
                event.from_instance,
                request_id,
                new_hops,
                target_eph_pk=target_eph_pk_b64,
            )
            return

        # 1) Loop prevention (relay-side only — see case 4 above).
        if request_id in self._seen_requests:
            return
        # Mark request_id as seen and cache caller for the response leg.
        self._seen_requests[request_id] = now + self._seen_ttl_s
        self._caller_cache[request_id] = (
            event.from_instance,
            now + self._seen_ttl_s,
        )

        # 5) Budget left → fan out to mesh-capable peers we haven't
        # bounced through yet (and skip the caller, who already has
        # this probe). The target itself, when it's one of our
        # confirmed peers, lands in this set and answers from its own
        # side — only the target can mint the ephemeral the seal step
        # needs.
        if len(new_hops) >= max_hops:
            return
        exclude = set(new_hops) | {event.from_instance}
        forward_targets = await self._mesh_capable_peers(exclude=exclude)
        for ft in forward_targets:
            try:
                await self._federation.send_event(
                    to_instance_id=ft.id,
                    event_type=FederationEventType.SPACE_FIND_ROUTE,
                    payload={
                        "request_id": request_id,
                        "target_instance_id": target,
                        "hops_traversed": new_hops,
                        "max_hops": max_hops,
                        "origin_instance_id": str(
                            p.get("origin_instance_id") or hops[0] if hops else "",
                        ),
                    },
                )
            except Exception:
                log.warning(
                    "route_discovery: forward FIND_ROUTE to %s failed (continuing)",
                    ft.id,
                    exc_info=True,
                )

    async def _on_route_found(self, event: "FederationEvent") -> None:
        """Forward a ROUTE_FOUND response back along the chain, or
        collect it if we're the origin.
        """
        p = event.payload
        request_id = str(p.get("request_id") or "")
        path_raw = p.get("path")
        path = [str(h) for h in path_raw] if isinstance(path_raw, list) else []
        target_eph_pk = str(p.get("target_eph_pk") or "")
        # Identity-binding fields the target minted (§D2). Relays carry
        # them opaquely; the origin verifies them below.
        target_identity_pk = str(p.get("target_identity_pk") or "")
        target_eph_sig = str(p.get("target_eph_sig") or "")
        if not request_id or not path or not target_eph_pk:
            return

        now = time.monotonic()
        self._prune_expired(now)

        # Origin path: collect this response, schedule the resolver
        # on the first hit so a fast response doesn't wait the full
        # timeout window.
        pending = self._pending.get(request_id)
        if pending is not None:
            if pending.resolved:
                return
            # SECURITY (§D2): authenticate the target ephemeral key
            # before trusting it. A confirmed relay can forge a
            # ROUTE_FOUND and substitute its OWN eph key; sealing space
            # content under it would hand the relay our plaintext. Drop
            # the response unless the route ends at the asked-for target
            # AND the eph key is signed by that target's identity.
            if not self._verify_route_found(
                request_id=request_id,
                target=pending.target,
                path=path,
                target_eph_pk=target_eph_pk,
                target_identity_pk=target_identity_pk,
                target_eph_sig=target_eph_sig,
                from_instance=event.from_instance,
            ):
                return
            pending.responses.append((path, target_eph_pk))
            if len(pending.responses) == 1:
                # First response — start the collection window.
                asyncio.create_task(self._resolve_after_window(request_id))
            return

        # Origin path, late answer: ``_pending`` is popped the instant the
        # wait resolves, so a ROUTE_FOUND that lands after the hard cap
        # (``discovery_timeout_s * 2``, 4 s by default) finds no pending
        # entry and used to be dropped on the floor. That is exactly what a
        # just-rebooted peer produces — its first round trip pays a DTLS
        # handshake and a STUN probe — and it left the origin with no route
        # while the answer it needed sat in its inbox. The response is still
        # valid, so verify it and warm the cache; the caller's next attempt
        # (next chunk, or the requester's next BEGIN) then resolves from
        # cache instead of re-flooding and timing out again.
        originated = self._origin_requests.get(request_id)
        if originated is not None:
            target, _expires = originated
            if self._verify_route_found(
                request_id=request_id,
                target=target,
                path=path,
                target_eph_pk=target_eph_pk,
                target_identity_pk=target_identity_pk,
                target_eph_sig=target_eph_sig,
                from_instance=event.from_instance,
            ):
                # Anchored on ARRIVAL, which is strictly after the target
                # minted the key, so our window still closes before its
                # private half expires.
                self._cache_route(
                    target_instance_id=target,
                    path=path,
                    target_eph_pk=target_eph_pk,
                    anchored_at=now,
                )
                log.info(
                    "route_discovery: late ROUTE_FOUND for %s cached"
                    " (arrived after the discovery window closed)",
                    target,
                )
            return

        # Relay path: forward to the caller we cached when the
        # original FIND_ROUTE flowed through us. The ``target_eph_pk``
        # the target minted travels through opaque — relays never
        # generate their own, and they pass the identity-binding
        # signature through unaltered so the origin can verify it.
        cached = self._caller_cache.get(request_id)
        if cached is None:
            return
        caller_id, _expires = cached
        try:
            await self._federation.send_event(
                to_instance_id=caller_id,
                event_type=FederationEventType.SPACE_ROUTE_FOUND,
                payload={
                    "request_id": request_id,
                    "path": path,
                    "target_eph_pk": target_eph_pk,
                    "target_identity_pk": target_identity_pk,
                    "target_eph_sig": target_eph_sig,
                },
            )
        except Exception:
            log.warning(
                "route_discovery: forward ROUTE_FOUND to %s failed",
                caller_id,
                exc_info=True,
            )

    # ── Helpers ────────────────────────────────────────────────────────

    async def _send_route_found(
        self,
        to_instance_id: str,
        request_id: str,
        path: list[str],
        *,
        target_eph_pk: str,
    ) -> None:
        # Bind the ephemeral key we just minted to OUR identity so the
        # origin can prove the key really came from the target it asked
        # for — a relay can't substitute its own eph (§D2 relay-MITM).
        target_identity_pk = self._federation.own_identity_pk.hex()
        target_eph_sig = b64url_encode(
            sign_ed25519(
                self._federation.own_identity_seed,
                _route_found_signing_bytes(request_id, target_eph_pk),
            )
        )
        try:
            await self._federation.send_event(
                to_instance_id=to_instance_id,
                event_type=FederationEventType.SPACE_ROUTE_FOUND,
                payload={
                    "request_id": request_id,
                    "path": path,
                    "target_eph_pk": target_eph_pk,
                    "target_identity_pk": target_identity_pk,
                    "target_eph_sig": target_eph_sig,
                },
            )
        except Exception:
            log.warning(
                "route_discovery: ROUTE_FOUND to %s failed",
                to_instance_id,
                exc_info=True,
            )

    def _verify_route_found(
        self,
        *,
        request_id: str,
        target: str,
        path: list[str],
        target_eph_pk: str,
        target_identity_pk: str,
        target_eph_sig: str,
        from_instance: str,
    ) -> bool:
        """Return ``True`` iff a ROUTE_FOUND response is authentic (§D2).

        Three independent checks, all of which must hold; any failure
        drops the response (logged at WARNING) so a forged or relayed
        substitution never reaches :meth:`_resolve_after_window`:

        1. ``path`` actually ends at the target we asked for.
        2. ``target_identity_pk`` hashes to that target's instance_id
           (``derive_instance_id`` — the id IS the identity-key
           fingerprint, so no prior pairing is needed to check this).
        3. ``target_eph_sig`` is a valid Ed25519 signature by that
           identity over ``_route_found_signing_bytes(request_id,
           target_eph_pk)`` — i.e. the genuine target signed THIS eph
           key for THIS request.

        Malformed hex / base64url input is treated as a verification
        failure rather than propagating, so a peer can't crash the
        origin with garbage fields.
        """
        if not path or path[-1] != target:
            log.warning(
                "route_discovery: ROUTE_FOUND from %s dropped — path does "
                "not end at target %s",
                from_instance,
                target,
            )
            return False
        if not target_identity_pk or not target_eph_sig:
            log.warning(
                "route_discovery: ROUTE_FOUND from %s dropped — missing "
                "identity-binding fields for target %s",
                from_instance,
                target,
            )
            return False
        try:
            identity_pk_bytes = bytes.fromhex(target_identity_pk)
            sig_bytes = b64url_decode(target_eph_sig)
        except ValueError, TypeError:
            log.warning(
                "route_discovery: ROUTE_FOUND from %s dropped — malformed "
                "identity_pk / signature encoding",
                from_instance,
            )
            return False
        try:
            derived = derive_instance_id(identity_pk_bytes)
        except ValueError:
            log.warning(
                "route_discovery: ROUTE_FOUND from %s dropped — identity_pk "
                "is not a valid Ed25519 key",
                from_instance,
            )
            return False
        if derived != target:
            log.warning(
                "route_discovery: ROUTE_FOUND from %s dropped — identity_pk "
                "derives to %s, not the requested target %s (substitution "
                "attempt?)",
                from_instance,
                derived,
                target,
            )
            return False
        if not verify_ed25519(
            identity_pk_bytes,
            _route_found_signing_bytes(request_id, target_eph_pk),
            sig_bytes,
        ):
            log.warning(
                "route_discovery: ROUTE_FOUND from %s dropped — bad eph-key "
                "signature for target %s",
                from_instance,
                target,
            )
            return False
        return True

    async def _resolve_after_window(self, request_id: str) -> None:
        """Wait ``discovery_timeout_s`` then resolve the pending Future
        with a random pick among the shortest-path responses (filtered
        to those carrying a non-empty ``target_eph_pk``).
        """
        await asyncio.sleep(self._discovery_timeout_s)
        pending = self._pending.get(request_id)
        if pending is None or pending.resolved:
            return
        pending.resolved = True
        # Defence in depth: drop responses that arrived without a
        # target_eph_pk (a sub-v_6 / malformed peer might ship one).
        valid = [(p, k) for (p, k) in pending.responses if k]
        if not valid:
            if not pending.future.done():
                pending.future.set_result(None)
            return
        shortest = min(len(p) for (p, _k) in valid)
        candidates = [(p, k) for (p, k) in valid if len(p) == shortest]
        # ``secrets.choice`` provides the random tie-break per the
        # design — random rotation across candidates spreads load
        # across equally-short relays and avoids pinning every
        # discovery on the lexicographically-first hop.
        picked_path, picked_eph = secrets.choice(candidates)
        if not pending.future.done():
            pending.future.set_result((list(picked_path), picked_eph))

    def _generate_target_eph(self, now: float) -> str:
        """Mint a fresh target-side ephemeral keypair, stash the priv
        keyed on the pub for later unseal lookups, and return the pub
        (base64url-encoded).
        """
        priv_b64, pub_b64 = routed_crypto.generate_ephemeral_keypair()
        self._target_eph_state[pub_b64] = (
            priv_b64,
            now + self._target_eph_ttl_s,
        )
        return pub_b64

    async def _mesh_capable_peers(
        self,
        *,
        exclude: set[str],
    ) -> list["RemoteInstance"]:
        """Return confirmed peers whose ``proto_version >= v_6``,
        minus anything in ``exclude``.
        """
        instances = await self._federation_repo.list_instances(
            status=PairingStatus.CONFIRMED.value,
        )
        out: list["RemoteInstance"] = []
        for inst in instances:
            if inst.id in exclude:
                continue
            if not await self._federation.peer_supports(
                inst.id,
                min_version=_MIN_MESH_PROTO_VERSION,
            ):
                continue
            out.append(inst)
        return out

    def _prune_expired(self, now: float) -> None:
        """Drop dedup / caller / route / target-eph entries whose TTL
        has elapsed.

        Each dict additionally has a hard size cap
        (:data:`_MAX_CACHE_ENTRIES`) so a peer that pumps unique
        ``request_id`` / ``target_eph_pk`` values faster than the TTL
        can't push us into unbounded memory growth (audit finding —
        defense-in-depth). At-cap behaviour is FIFO: the
        oldest-by-expiry entries are evicted to make room.
        """
        if self._seen_requests:
            self._seen_requests = cap_by_expiry(
                {k: v for k, v in self._seen_requests.items() if v > now},
                key=lambda kv: kv[1],
            )
        if self._caller_cache:
            self._caller_cache = cap_by_expiry(
                {k: v for k, v in self._caller_cache.items() if v[1] > now},
                key=lambda kv: kv[1][1],
            )
        if self._route_cache:
            self._route_cache = cap_by_expiry(
                {k: v for k, v in self._route_cache.items() if v.expires_at > now},
                key=lambda kv: kv[1].expires_at,
            )
        if self._target_eph_state:
            self._target_eph_state = cap_by_expiry(
                {k: v for k, v in self._target_eph_state.items() if v[1] > now},
                key=lambda kv: kv[1][1],
            )
        if self._origin_requests:
            self._origin_requests = cap_by_expiry(
                {k: v for k, v in self._origin_requests.items() if v[1] > now},
                key=lambda kv: kv[1][1],
            )
        if self._negative_until:
            # Same defense-in-depth as the dicts above: a caller asking us
            # to route to a stream of unknown targets would otherwise grow
            # this one without bound, since every miss adds an entry.
            self._negative_until = cap_by_expiry(
                {k: v for k, v in self._negative_until.items() if v > now},
                key=lambda kv: kv[1],
            )

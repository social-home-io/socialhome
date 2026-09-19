"""Tests for :class:`RouteDiscoveryService` (§D2 PR 2 / v_6 mesh).

Covers:

* Local short-circuit: ``target == self`` resolves synchronously with
  a freshly-minted target ephemeral.
* Direct-peer discovery: a CONFIRMED v_6 peer resolves via one probe,
  returning ``([self, peer], target_eph_pk)``.
* Indirect discovery: a small graph ``a—b—c`` finds ``([a, b, c],
  target_eph_pk)``.
* Loop prevention: a probe with a known ``request_id`` is dropped.
* Cycle prevention: a probe with self in ``hops_traversed`` is dropped.
* Hop budget: ``max_hops=2`` on a depth-3 target returns ``None``.
* Cache hit: second discovery for the same target within TTL skips
  the probe entirely.
* Random tie-break: when two equally-short paths land, both pick
  variants appear over N runs.
* Sub-v_6 peers are filtered out of relay forwarding + can't act as
  the target (no v_6 means no ephemeral mint).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    PairingStatus,
    InstanceSource,
)
from socialhome.federation import route_discovery, routed_crypto
from socialhome.federation.route_discovery import (
    ROUTE_CACHE_SAFETY_MARGIN_S,
    ROUTE_CACHE_TTL_S,
    RouteDiscoveryService,
    _CachedRoute,
    _PendingDiscovery,
    _route_found_signing_bytes,
)


# ── Test doubles ──────────────────────────────────────────────────────


@dataclass
class _FakeInstance:
    """Stand-in for :class:`RemoteInstance`."""

    id: str
    status: PairingStatus = PairingStatus.CONFIRMED
    proto_version: int = 6
    source: InstanceSource = InstanceSource.MANUAL


class _FakeFederationRepo:
    def __init__(self, instances: dict[str, _FakeInstance] | None = None) -> None:
        self._instances: dict[str, _FakeInstance] = instances or {}

    async def get_instance(self, instance_id: str) -> _FakeInstance | None:
        return self._instances.get(instance_id)

    async def list_social_instances(self):
        # Mirrors ``SqliteFederationRepo.list_social_instances``: CONFIRMED
        # rows minus the space-scoped ``space_session`` ones. A fake that
        # returned the same rows for both methods would make the mesh's
        # peer-class filter untestable here.
        return [
            i
            for i in await self.list_instances(status="confirmed")
            if i.source is not InstanceSource.SPACE_SESSION
        ]

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[_FakeInstance]:
        out = list(self._instances.values())
        if status is not None:
            out = [i for i in out if i.status.value == status]
        return out


@dataclass
class _Node:
    """One instance in the simulated mesh.

    Holds the federation repo for the node, the wired-up route
    discovery service, and a dict of neighbour nodes (``id ->
    _Node``) that the test driver uses to physically deliver
    envelopes from one node to another.
    """

    instance_id: str
    repo: _FakeFederationRepo
    fed: "_LinkedFederationService"
    service: RouteDiscoveryService
    neighbours: dict[str, "_Node"] = field(default_factory=dict)


class _LinkedFederationService:
    """A test ``FederationService`` shim.

    Each node has one of these. ``send_event`` dispatches the envelope
    straight onto the destination node's :class:`RouteDiscoveryService`
    so the round-trip runs in-memory without any real transport.

    Tracks every outbound for assertions.
    """

    def __init__(
        self,
        *,
        own_instance_id: str,
        repo: _FakeFederationRepo,
        own_identity_seed: bytes,
        own_identity_pk: bytes,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._repo = repo
        self._own_identity_seed = own_identity_seed
        self._own_identity_pk = own_identity_pk
        #: dest_instance_id → other node (set by the test driver).
        self.peers: dict[str, _Node] = {}
        self.sent: list[dict[str, Any]] = []
        # Optional bytewise hook so a test can force a send to fail.
        self.fail_to: set[str] = set()

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    @property
    def own_identity_seed(self) -> bytes:
        return self._own_identity_seed

    @property
    def own_identity_pk(self) -> bytes:
        return self._own_identity_pk

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        peer = await self._repo.get_instance(instance_id)
        if peer is None:
            return False
        return peer.proto_version >= min_version

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> Any:
        self.sent.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        if to_instance_id in self.fail_to:
            raise RuntimeError(f"forced send failure to {to_instance_id}")
        dest = self.peers.get(to_instance_id)
        if dest is None:
            return SimpleNamespace(ok=False, instance_id=to_instance_id)
        ev = FederationEvent(
            msg_id=f"m-{event_type.value}-{len(self.sent)}",
            event_type=event_type,
            from_instance=self._own_instance_id,
            to_instance=to_instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=payload,
        )

        # Run the destination handler in a background task so the
        # caller's ``await`` returns immediately (mirrors the real
        # transport's fire-and-forget shape).
        async def _deliver() -> None:
            handler_map = {
                FederationEventType.SPACE_FIND_ROUTE: (dest.service._on_find_route),
                FederationEventType.SPACE_ROUTE_FOUND: (dest.service._on_route_found),
            }
            handler = handler_map.get(event_type)
            if handler is not None:
                await handler(ev)

        asyncio.create_task(_deliver())
        return SimpleNamespace(ok=True, instance_id=to_instance_id)


def _build_mesh(
    graph: dict[str, list[str]],
    *,
    proto_versions: dict[str, int] | None = None,
    discovery_timeout_s: float = 0.05,
    max_hops: int = 3,
) -> dict[str, _Node]:
    """Build a network of nodes following the adjacency map.

    ``graph[n]`` lists ``n``'s confirmed neighbours. Each neighbour
    relationship is bidirectional — listing ``a -> [b]`` in the map
    implies a sees b on its repo + b sees a on its repo.

    Each peer entry is rendered into both nodes' ``RemoteInstance``
    rows via ``_FakeFederationRepo``. ``proto_versions[node_id]``
    overrides the default ``proto_version=6`` for a specific node.

    Each node is given a *real* Ed25519 identity keypair and its
    ``instance_id`` is derived from that key
    (:func:`derive_instance_id`) — required so the origin's
    authenticated-route-discovery check (``derive_instance_id(target
    identity pk) == target``) holds end-to-end through the mesh. The
    returned dict is still keyed by the human-readable graph name for
    test ergonomics; ``_Node.instance_id`` carries the derived id.
    """
    proto_versions = proto_versions or {}
    # Readable name → (seed, pk, derived instance_id).
    identities: dict[str, tuple[bytes, bytes, str]] = {}
    for name in graph:
        kp = generate_identity_keypair()
        identities[name] = (
            kp.private_key,
            kp.public_key,
            derive_instance_id(kp.public_key),
        )

    nodes: dict[str, _Node] = {}
    for name in graph:
        seed, pk, inst_id = identities[name]
        repo = _FakeFederationRepo()
        fed = _LinkedFederationService(
            own_instance_id=inst_id,
            repo=repo,
            own_identity_seed=seed,
            own_identity_pk=pk,
        )
        service = RouteDiscoveryService(
            federation_service=fed,  # type: ignore[arg-type]
            federation_repo=repo,  # type: ignore[arg-type]
            max_hops=max_hops,
            discovery_timeout_s=discovery_timeout_s,
            cache_ttl_s=60.0,
            seen_ttl_s=60.0,
        )
        nodes[name] = _Node(
            instance_id=inst_id,
            repo=repo,
            fed=fed,
            service=service,
        )
    # Wire neighbour repos + peer maps (keyed by derived instance id).
    for name, neighbour_names in graph.items():
        node = nodes[name]
        for nb_name in neighbour_names:
            nb = nodes[nb_name]
            pv = proto_versions.get(nb_name, 6)
            node.repo._instances[nb.instance_id] = _FakeInstance(
                id=nb.instance_id,
                status=PairingStatus.CONFIRMED,
                proto_version=pv,
            )
            node.fed.peers[nb.instance_id] = nb
            node.neighbours[nb.instance_id] = nb
    return nodes


# ── Tests ─────────────────────────────────────────────────────────────


async def test_local_target_returns_self_path():
    """A request to discover the route to *our own* instance returns
    ``([self], target_eph_pk)`` without any probe — and the target
    ephemeral priv is cached on us for the inbound SPACE_ROUTED."""
    nodes = _build_mesh({"a": []})
    a_id = nodes["a"].instance_id
    result = await nodes["a"].service.discover_route(a_id)
    assert result is not None
    path, target_eph_pk = result
    assert path == [a_id]
    assert target_eph_pk  # non-empty b64url
    # The corresponding priv is cached locally for unseal.
    assert nodes["a"].service.lookup_target_eph_priv(target_eph_pk) is not None
    # No FIND_ROUTE shipped.
    assert nodes["a"].fed.sent == []


async def test_direct_peer_resolves_via_single_hop_probe():
    """A confirmed direct v_6 peer resolves through a one-hop probe
    (no static short-circuit): the target answers with its freshly-
    minted ephemeral pub, so the origin can seal against it."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    a_id, b_id = nodes["a"].instance_id, nodes["b"].instance_id
    result = await nodes["a"].service.discover_route(b_id)
    assert result is not None
    path, target_eph_pk = result
    assert path == [a_id, b_id]
    assert target_eph_pk
    # The target ('b') minted the priv and cached it for unseal.
    assert nodes["b"].service.lookup_target_eph_priv(target_eph_pk) is not None


async def test_indirect_discovery_finds_three_hop_path():
    """In ``a — b — c`` (a paired with b, b paired with c, a NOT
    paired with c), discovering c from a yields ``([a, b, c],
    target_eph_pk)``."""
    nodes = _build_mesh({"a": ["b"], "b": ["a", "c"], "c": ["b"]})
    a_id, b_id, c_id = (
        nodes["a"].instance_id,
        nodes["b"].instance_id,
        nodes["c"].instance_id,
    )
    result = await nodes["a"].service.discover_route(c_id)
    assert result is not None
    path, target_eph_pk = result
    assert path == [a_id, b_id, c_id]
    assert target_eph_pk
    assert nodes["c"].service.lookup_target_eph_priv(target_eph_pk) is not None


async def test_loop_prevention_drops_duplicate_request_id():
    """A relay that has already seen a ``request_id`` drops the
    inbound FIND_ROUTE silently."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc_b = nodes["b"].service
    svc_b._seen_requests["rid-x"] = float("inf")
    fed_b = nodes["b"].fed
    fed_b.sent.clear()
    ev = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-x",
            "target_instance_id": "z",
            "hops_traversed": ["a"],
            "max_hops": 3,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    assert fed_b.sent == []


async def test_cycle_prevention_drops_self_in_hops_traversed():
    """If ``hops_traversed`` already includes us, drop — the chain
    has looped back through our node."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    fed_b = nodes["b"].fed
    ev = FederationEvent(
        msg_id="m-cycle",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="x",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-cycle",
            "target_instance_id": "z",
            "hops_traversed": ["a", "b", "c"],  # self ("b") present
            "max_hops": 3,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    assert fed_b.sent == []


async def test_max_hops_budget_returns_none_when_target_too_deep():
    """With ``max_hops=2`` and target at depth 3 (``a → b → c → d``),
    no relay forwards far enough to reach d — returns ``None``."""
    nodes = _build_mesh(
        {"a": ["b"], "b": ["a", "c"], "c": ["b", "d"], "d": ["c"]},
        discovery_timeout_s=0.05,
        max_hops=2,
    )
    result = await nodes["a"].service.discover_route(nodes["d"].instance_id)
    assert result is None


async def test_cache_hit_on_second_discover():
    """A second ``discover_route`` for the same target within the
    cache TTL returns the cached ``(path, target_eph_pk)`` without
    re-running the probe.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    a_id, b_id = nodes["a"].instance_id, nodes["b"].instance_id
    first = await nodes["a"].service.discover_route(b_id)
    assert first is not None
    first_path, first_eph = first
    assert first_path == [a_id, b_id]
    assert b_id in nodes["a"].service._route_cache
    # Force a third-party look — drop b from the repo + retry. If the
    # cache is honored, we still get the *same* tuple back.
    nodes["a"].repo._instances.pop(b_id, None)
    nodes["a"].fed.peers.pop(b_id, None)
    cached = await nodes["a"].service.discover_route(b_id)
    assert cached == ([a_id, b_id], first_eph)


async def test_invalidate_drops_cache_entry():
    """``invalidate`` removes a cached path; subsequent discover
    runs a fresh probe."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    b_id = nodes["b"].instance_id
    await nodes["a"].service.discover_route(b_id)
    assert b_id in nodes["a"].service._route_cache
    await nodes["a"].service.invalidate(b_id)
    assert b_id not in nodes["a"].service._route_cache


async def test_random_tie_break_over_equal_paths():
    """Two equally-short paths a→b→z and a→c→z — over N runs both
    relays appear as the first hop."""
    nodes = _build_mesh(
        {
            "a": ["b", "c"],
            "b": ["a", "z"],
            "c": ["a", "z"],
            "z": ["b", "c"],
        },
        # The collection window starts on the first response; both
        # branches need to land before it fires for tie-breaking to
        # actually have anything to break. Use a generous window so
        # we're testing the secrets.choice tie-break, not the
        # asyncio scheduler's response latency.
        discovery_timeout_s=0.2,
    )
    a_id, b_id, c_id, z_id = (
        nodes["a"].instance_id,
        nodes["b"].instance_id,
        nodes["c"].instance_id,
        nodes["z"].instance_id,
    )
    seen_first_hops: set[str] = set()
    for _ in range(20):
        # Clear cache so each iteration runs a fresh probe.
        await nodes["a"].service.invalidate(z_id)
        result = await nodes["a"].service.discover_route(z_id)
        assert result is not None
        path, _eph = result
        # Path is [a, ?, z] — capture the middle hop.
        assert len(path) == 3
        assert path[0] == a_id
        assert path[-1] == z_id
        seen_first_hops.add(path[1])
        if len(seen_first_hops) == 2:
            break
    assert seen_first_hops == {b_id, c_id}, (
        f"random tie-break didn't sample both hops in 20 runs: {seen_first_hops}"
    )


async def test_sub_v6_peer_is_not_proposed_as_next_hop():
    """A v_5 neighbour is filtered out of relay forwarding: in the
    graph ``a — b(v5) — c`` discovering ``c`` returns ``None`` —
    a cannot reach c because b is mesh-incapable.
    """
    nodes = _build_mesh(
        {"a": ["b"], "b": ["a", "c"], "c": ["b"]},
        proto_versions={"b": 5},
        discovery_timeout_s=0.05,
    )
    result = await nodes["a"].service.discover_route(nodes["c"].instance_id)
    assert result is None


async def test_space_session_peer_is_never_a_mesh_hop():
    """A household we met through an invite link is not a mesh node. In
    ``a — b(space_session) — c`` discovering ``c`` returns ``None``: b is
    neither proposed as a next hop nor flooded with our probe, because
    ``_mesh_capable_peers`` reads ``list_social_instances()``. (A
    ``SPACE_ROUTE_FOUND`` carries ``path`` — every relay's id — so a
    link-joined stranger acting as a hop would map our social graph.)"""
    nodes = _build_mesh(
        {"a": ["b"], "b": ["a", "c"], "c": ["b"]},
        discovery_timeout_s=0.05,
    )
    repo = nodes["a"].service._federation_repo
    repo._instances[nodes["b"].instance_id].source = InstanceSource.SPACE_SESSION
    capable = await nodes["a"].service._mesh_capable_peers(exclude=set())
    assert nodes["b"].instance_id not in [i.id for i in capable]
    assert await nodes["a"].service.discover_route(nodes["c"].instance_id) is None


async def test_sub_v6_direct_peer_target_returns_none():
    """A v_5 direct peer can't be reached via the BFS probe (we won't
    propose them as a relay), and there's no static short-circuit
    anymore — so discovery returns ``None``."""
    nodes = _build_mesh(
        {"a": ["b"], "b": ["a"]},
        proto_versions={"b": 5},
        discovery_timeout_s=0.05,
    )
    result = await nodes["a"].service.discover_route(nodes["b"].instance_id)
    assert result is None


async def test_no_confirmed_peers_returns_none_immediately():
    """An origin with zero confirmed peers can't probe — returns
    ``None`` without shipping anything."""
    nodes = _build_mesh({"a": []})
    result = await nodes["a"].service.discover_route("missing")
    assert result is None
    assert nodes["a"].fed.sent == []


async def test_relay_forwards_route_found_back_to_caller():
    """A relay (``b``) that previously cached the caller (``a``) for
    a ``request_id`` forwards the inbound ROUTE_FOUND back to a,
    propagating the target's ephemeral pub through opaque.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a", "c"], "c": ["b"]})
    svc_b = nodes["b"].service
    # Mark request as having traversed a→b (so b knows the caller).
    svc_b._caller_cache["rid-fwd"] = ("a", float("inf"))
    svc_b._seen_requests["rid-fwd"] = float("inf")
    nodes["b"].fed.sent.clear()
    # Now c → b ROUTE_FOUND with the discovered path + target eph.
    ev = FederationEvent(
        msg_id="m-rf",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="c",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-fwd",
            "path": ["a", "b", "c"],
            "target_eph_pk": "fake-pub-b64",
            "target_identity_pk": "deadbeef",
            "target_eph_sig": "sig-b64",
        },
    )
    await svc_b._on_route_found(ev)
    # b should have forwarded to a.
    assert len(nodes["b"].fed.sent) == 1
    fwd = nodes["b"].fed.sent[0]
    assert fwd["to"] == "a"
    assert fwd["event_type"] == FederationEventType.SPACE_ROUTE_FOUND
    assert fwd["payload"]["path"] == ["a", "b", "c"]
    # Relay never strips / replaces the target ephemeral or its
    # identity-binding signature — they travel through opaquely.
    assert fwd["payload"]["target_eph_pk"] == "fake-pub-b64"
    assert fwd["payload"]["target_identity_pk"] == "deadbeef"
    assert fwd["payload"]["target_eph_sig"] == "sig-b64"


async def test_route_found_without_target_eph_is_dropped():
    """A ROUTE_FOUND missing the ``target_eph_pk`` field is dropped —
    we can't seal against an empty pub, so a relay propagating it
    would just deliver an unusable path to the origin."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc_b = nodes["b"].service
    svc_b._caller_cache["rid-missing-eph"] = ("a", float("inf"))
    svc_b._seen_requests["rid-missing-eph"] = float("inf")
    nodes["b"].fed.sent.clear()
    ev = FederationEvent(
        msg_id="m-rf-bad",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="c",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={"request_id": "rid-missing-eph", "path": ["a", "b", "c"]},
    )
    await svc_b._on_route_found(ev)
    assert nodes["b"].fed.sent == []


async def test_orphan_route_found_is_silently_dropped():
    """A ROUTE_FOUND for an unknown ``request_id`` (no pending, no
    caller cache) is dropped without raising."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-orphan",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="x",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "no-such",
            "path": ["x", "b"],
            "target_eph_pk": "fake-pub-b64",
        },
    )
    await svc_b._on_route_found(ev)  # no exception
    assert nodes["b"].fed.sent == []


async def test_find_route_with_missing_request_id_is_dropped():
    """Defensive: a FIND_ROUTE without ``request_id`` doesn't trip
    any forwarding logic."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-bad",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={"target_instance_id": "z", "hops_traversed": ["a"]},
    )
    await svc_b._on_find_route(ev)
    assert nodes["b"].fed.sent == []


async def test_attach_to_wires_two_handlers():
    """`attach_to` registers FIND_ROUTE + ROUTE_FOUND handlers."""

    class _FakeRegistry:
        def __init__(self) -> None:
            self.bindings: dict[FederationEventType, list] = {}

        def register(self, et, handler):
            self.bindings.setdefault(et, []).append(handler)

    class _FakeFedSvc:
        def __init__(self) -> None:
            self._event_registry = _FakeRegistry()
            self._own_instance_id = "me"

        @property
        def own_instance_id(self) -> str:
            return self._own_instance_id

    fed_svc = _FakeFedSvc()
    repo = _FakeFederationRepo()
    svc = RouteDiscoveryService(
        federation_service=fed_svc,  # type: ignore[arg-type]
        federation_repo=repo,  # type: ignore[arg-type]
    )
    svc.attach_to(fed_svc)  # type: ignore[arg-type]
    assert FederationEventType.SPACE_FIND_ROUTE in fed_svc._event_registry.bindings
    assert FederationEventType.SPACE_ROUTE_FOUND in fed_svc._event_registry.bindings


@pytest.mark.asyncio(loop_scope="function")
async def test_max_hops_relay_drops_when_hops_traversed_at_limit():
    """A relay that receives a probe whose ``hops_traversed`` already
    has length ``max_hops`` doesn't forward (budget exhausted)."""
    nodes = _build_mesh({"b": []}, max_hops=2)
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-budget",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-budget",
            "target_instance_id": "z",
            "hops_traversed": ["a"],  # adding self brings us to 2 = max_hops
            "max_hops": 2,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    # No forward to a peer named 'z' (we don't have one) AND no
    # forward to anyone else either — len(new_hops) == max_hops.
    assert nodes["b"].fed.sent == []


# ── Additional edge-case coverage ─────────────────────────────────────


async def test_discover_route_returns_none_when_every_send_fails(caplog):
    """If every confirmed peer's ``send_event`` raises during the
    initial fan-out, ``sent == 0`` and discover returns None with no
    pending state left behind."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    nodes["a"].fed.fail_to = {"b"}
    result = await nodes["a"].service.discover_route("z")
    assert result is None
    # No pending entry leaked (the cleanup branch fired).
    assert nodes["a"].service._pending == {}


async def test_discover_route_continues_when_one_of_many_sends_fails():
    """If one of several peers' send_event raises but at least one
    succeeds, the discovery proceeds (and times out → returns None
    because the failing peer's branch never produced a response)."""
    nodes = _build_mesh(
        {"a": ["b", "x"], "b": ["a"], "x": ["a"]},
        discovery_timeout_s=0.05,
    )
    b_id, x_id = nodes["b"].instance_id, nodes["x"].instance_id
    # Force the ship to 'x' to fail. 'b' is reachable but isn't the
    # target — discovery still resolves to None for unknown 'z'.
    nodes["a"].fed.fail_to = {x_id}
    result = await nodes["a"].service.discover_route("z")
    assert result is None
    # send tried both peers; ≥1 succeeded.
    assert any(s["to"] == b_id for s in nodes["a"].fed.sent)


async def test_lookup_target_eph_priv_unknown_pub_returns_none():
    """Unknown pub → None."""
    nodes = _build_mesh({"a": []})
    assert nodes["a"].service.lookup_target_eph_priv("not-a-real-pub") is None


async def test_lookup_target_eph_priv_expired_entry_returns_none_and_reaps():
    """An expired entry is lazily reaped on lookup."""
    nodes = _build_mesh({"a": []})
    svc = nodes["a"].service
    svc._target_eph_state["expired-pub"] = (
        "priv-data",
        time.monotonic() - 1.0,  # already expired
    )
    assert svc.lookup_target_eph_priv("expired-pub") is None
    # Reaped from state.
    assert "expired-pub" not in svc._target_eph_state


async def test_find_route_with_missing_target_is_dropped():
    """A FIND_ROUTE without ``target_instance_id`` is dropped (the
    missing request_id / target log path)."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-no-target",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-no-target",
            "hops_traversed": ["a"],
            "max_hops": 3,
            "origin_instance_id": "a",
        },
    )
    await svc_b._on_find_route(ev)
    assert nodes["b"].fed.sent == []


async def test_find_route_with_malformed_max_hops_falls_back_to_local_cap():
    """A FIND_ROUTE whose ``max_hops`` is not an int gracefully falls
    back to the local cap (no crash)."""
    nodes = _build_mesh({"a": ["b"], "b": ["a", "c"], "c": ["b"]}, max_hops=3)
    a_id, c_id = nodes["a"].instance_id, nodes["c"].instance_id
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-bad-mh",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance=a_id,
        to_instance=nodes["b"].instance_id,
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-bad-mh",
            "target_instance_id": c_id,
            "hops_traversed": [a_id],
            "max_hops": {"not": "an int"},  # triggers TypeError → fallback
            "origin_instance_id": a_id,
        },
    )
    await svc_b._on_find_route(ev)
    # b forwards to c (its only mesh-capable peer other than the caller).
    forwards = [
        s
        for s in nodes["b"].fed.sent
        if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
    ]
    assert any(s["to"] == c_id for s in forwards)


async def test_find_route_forward_send_failure_is_logged(caplog):
    """Relay-side forwarding: when ``send_event`` raises on the
    forward FIND_ROUTE, we swallow + warn."""
    nodes = _build_mesh({"a": ["b"], "b": ["a", "c"], "c": ["b"]})
    a_id, c_id = nodes["a"].instance_id, nodes["c"].instance_id
    svc_b = nodes["b"].service
    nodes["b"].fed.fail_to = {c_id}
    ev = FederationEvent(
        msg_id="m-fwd-fail",
        event_type=FederationEventType.SPACE_FIND_ROUTE,
        from_instance=a_id,
        to_instance=nodes["b"].instance_id,
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-fwd-fail",
            "target_instance_id": "z",  # unknown, so b must forward
            "hops_traversed": [a_id],
            "max_hops": 3,
            "origin_instance_id": a_id,
        },
    )
    await svc_b._on_find_route(ev)
    # We attempted the send and the warning fired.
    assert any(
        f"forward FIND_ROUTE to {c_id} failed" in r.message for r in caplog.records
    )


async def test_on_route_found_resolved_pending_is_ignored():
    """A ROUTE_FOUND that arrives after the pending entry was already
    marked resolved is a no-op (the future may already be done)."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str, str] | None] = loop.create_future()
    pending = _PendingDiscovery(future=fut, target="b")
    pending.resolved = True  # already decided
    svc_a._pending["rid-late"] = pending
    ev = FederationEvent(
        msg_id="m-late",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-late",
            "path": ["a", "b"],
            "target_eph_pk": "pub-x",
        },
    )
    await svc_a._on_route_found(ev)
    # Pending still has zero responses appended (early-return fired).
    assert pending.responses == []
    # Future remains pending; cancel it so no warning at teardown.
    fut.cancel()


async def test_on_route_found_unknown_request_id_relay_branch_is_silent():
    """Relay branch: a ROUTE_FOUND for a request_id we never relayed
    is dropped (no pending, no caller_cache)."""
    nodes = _build_mesh({"b": []})
    svc_b = nodes["b"].service
    ev = FederationEvent(
        msg_id="m-relay-orphan",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="c",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "never-cached",
            "path": ["a", "b", "c"],
            "target_eph_pk": "pub-x",
        },
    )
    await svc_b._on_route_found(ev)
    assert nodes["b"].fed.sent == []


async def test_on_route_found_relay_send_failure_is_logged(caplog):
    """Relay forwarding ROUTE_FOUND back to the cached caller: if
    ``send_event`` raises, swallow + warn."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc_b = nodes["b"].service
    svc_b._caller_cache["rid-fail"] = ("a", float("inf"))
    nodes["b"].fed.fail_to = {"a"}
    ev = FederationEvent(
        msg_id="m-rf-fail",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="c",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "request_id": "rid-fail",
            "path": ["a", "b", "c"],
            "target_eph_pk": "pub-x",
        },
    )
    await svc_b._on_route_found(ev)
    assert any("forward ROUTE_FOUND to a failed" in r.message for r in caplog.records)


async def test_send_route_found_failure_is_logged(caplog):
    """``_send_route_found`` swallows ``send_event`` failures with a
    warning rather than propagating — used on the local-target branch
    of FIND_ROUTE."""
    # Build a single-node mesh and call _send_route_found directly.
    nodes = _build_mesh({"a": []})
    svc_a = nodes["a"].service
    # Configure the fed shim to fail on any to_instance_id.
    nodes["a"].fed.fail_to = {"x"}
    await svc_a._send_route_found(
        "x",
        "rid-srf",
        ["x", "a"],
        target_eph_pk="pub-x",
    )
    assert any("ROUTE_FOUND to x failed" in r.message for r in caplog.records)


async def test_resolve_after_window_no_responses_resolves_to_none():
    """If the collection window fires with zero responses bearing a
    target_eph_pk, the future resolves to None (defence in depth)."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str, str] | None] = loop.create_future()
    pending = _PendingDiscovery(future=fut, target="t")
    # A single response without a target_eph_pk — gets filtered out.
    pending.responses.append((["a", "t"], "", "pk-t"))
    svc_a._pending["rid-empty"] = pending
    await svc_a._resolve_after_window("rid-empty")
    assert pending.resolved is True
    assert fut.done()
    assert fut.result() is None


async def test_resolve_after_window_pending_missing_is_noop():
    """If the pending entry was already cleaned up, the window
    resolver is a no-op (no AttributeError)."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    # _resolve_after_window simply returns when pending is None.
    await svc_a._resolve_after_window("nonexistent-rid")  # no exception


async def test_resolve_after_window_skips_set_result_when_future_already_done():
    """If the future was resolved out-of-band, ``set_result`` is
    skipped (avoids the "future already done" assertion)."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str, str] | None] = loop.create_future()
    fut.set_result(None)  # already done
    pending = _PendingDiscovery(future=fut, target="t")
    pending.responses.append((["a", "b"], "pub-x", "pk-b"))
    svc_a._pending["rid-done"] = pending
    # Should not raise even though the future is already done.
    await svc_a._resolve_after_window("rid-done")
    assert pending.resolved is True


async def test_resolve_after_window_no_valid_skips_set_result_when_done():
    """``not valid`` branch with the future already done: ``set_result``
    is skipped (covers the ``if not pending.future.done():`` False arm
    inside the empty-valid branch)."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str, str] | None] = loop.create_future()
    fut.set_result(None)  # already done
    pending = _PendingDiscovery(future=fut, target="t")
    # No valid responses (every response missing target_eph_pk).
    pending.responses.append((["a", "t"], "", "pk-t"))
    svc_a._pending["rid-novalid-done"] = pending
    await svc_a._resolve_after_window("rid-novalid-done")
    assert pending.resolved is True
    # Future still holds its original result; no exception raised.
    assert fut.result() is None


async def test_prune_expired_drops_expired_entries():
    """``_prune_expired`` reaps all four caches: seen_requests,
    caller_cache, route_cache, and target_eph_state."""
    nodes = _build_mesh({"a": []})
    svc = nodes["a"].service
    now = time.monotonic()
    # Seed expired entries in every cache.
    svc._seen_requests["old-req"] = now - 1.0
    svc._seen_requests["fresh-req"] = now + 60.0
    svc._caller_cache["old-call"] = ("peer", now - 1.0)
    svc._caller_cache["fresh-call"] = ("peer", now + 60.0)
    svc._route_cache["old-tgt"] = _CachedRoute(
        path=["a"], target_eph_pk="x", target_identity_pk="pk-x", expires_at=now - 1.0
    )
    svc._route_cache["fresh-tgt"] = _CachedRoute(
        path=["a"], target_eph_pk="y", target_identity_pk="pk-y", expires_at=now + 60.0
    )
    svc._target_eph_state["old-pub"] = ("priv-1", now - 1.0)
    svc._target_eph_state["fresh-pub"] = ("priv-2", now + 60.0)
    svc._prune_expired(now)
    assert set(svc._seen_requests) == {"fresh-req"}
    assert set(svc._caller_cache) == {"fresh-call"}
    assert set(svc._route_cache) == {"fresh-tgt"}
    assert set(svc._target_eph_state) == {"fresh-pub"}


async def test_mesh_capable_peers_filters_unsupported(monkeypatch):
    """A confirmed peer whose ``peer_supports`` returns False is
    filtered out — the ``continue`` path."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    fed_a = nodes["a"].fed

    async def _not_supported(instance_id: str, *, min_version: int) -> bool:
        return False

    monkeypatch.setattr(fed_a, "peer_supports", _not_supported)
    peers = await nodes["a"].service._mesh_capable_peers(exclude=set())
    assert peers == []


def test_cap_by_expiry_keeps_newest():
    """``cap_by_expiry`` evicts oldest-by-expiry first — defense-in-
    depth against an attacker pumping unique entries faster than the
    TTL window can prune them."""
    from socialhome.federation.route_discovery import cap_by_expiry

    items = {f"k{i}": (None, float(i)) for i in range(10)}
    capped = cap_by_expiry(items, key=lambda kv: kv[1][1], cap=3)
    assert set(capped) == {"k7", "k8", "k9"}


def test_cap_by_expiry_below_cap_is_identity():
    """No-op when under the cap — the hot path is the common case."""
    from socialhome.federation.route_discovery import cap_by_expiry

    items = {"a": (None, 1.0), "b": (None, 2.0)}
    capped = cap_by_expiry(items, key=lambda kv: kv[1][1], cap=10)
    assert capped is items


def test_prune_caps_oversized_target_eph_state():
    """Even with a hostile peer pumping unique probes the
    target-side ephemeral state dict stays bounded by
    ``_MAX_CACHE_ENTRIES``."""
    from socialhome.federation.route_discovery import _MAX_CACHE_ENTRIES

    repo = _FakeFederationRepo()
    svc = RouteDiscoveryService(
        federation_service=SimpleNamespace(own_instance_id="self"),  # type: ignore[arg-type]
        federation_repo=repo,  # type: ignore[arg-type]
    )
    now = time.monotonic()
    overshoot = 50  # 5050 vs 5000 — fast enough.
    cap = _MAX_CACHE_ENTRIES
    for i in range(cap + overshoot):
        svc._target_eph_state[f"pub{i}"] = (f"priv{i}", now + 60.0 + i)
    svc._prune_expired(now)
    assert len(svc._target_eph_state) == cap
    assert "pub0" not in svc._target_eph_state
    assert f"pub{cap + overshoot - 1}" in svc._target_eph_state


# ── §D2 authenticated route discovery — relay-MITM regression guards ───
#
# The mesh route-discovery origin used to trust the ``target_eph_pk`` it
# pulled out of a *relayed*, unauthenticated SPACE_ROUTE_FOUND. A
# malicious confirmed peer that caught the SPACE_FIND_ROUTE flood could
# answer ROUTE_FOUND(path=[origin, attacker], target_eph_pk=<attacker's
# own eph>) and win the shortest-path tie-break, so the origin sealed
# real space content under the *attacker's* key → the attacker decrypts
# it (the inner payload is NOT independently encrypted on the mesh
# path). The fix binds ``target_eph_pk`` to the target's identity key:
# the genuine target signs the eph key, and the origin verifies
# ``derive_instance_id(target_identity_pk) == target`` + a valid sig
# over THIS request_id before collecting the response.


def _signed_route_found_payload(
    *,
    request_id: str,
    path: list[str],
    target_eph_pk_b64: str,
    signer_seed: bytes,
    signer_pk: bytes,
) -> dict:
    """Build a SPACE_ROUTE_FOUND payload signed by ``signer`` — used to
    forge both an attacker's self-signed response and a legit target's
    response (the difference is purely which identity signs)."""
    sig = sign_ed25519(
        signer_seed,
        _route_found_signing_bytes(request_id, target_eph_pk_b64),
    )
    return {
        "request_id": request_id,
        "path": path,
        "target_eph_pk": target_eph_pk_b64,
        "target_identity_pk": signer_pk.hex(),
        "target_eph_sig": b64url_encode(sig),
    }


async def _drive_origin_route_found(
    origin: _Node,
    *,
    target_instance_id: str,
    payload: dict,
    discovery_timeout_s: float = 0.01,
):
    """Seed a pending discovery on ``origin`` for ``target_instance_id``,
    feed it a single ROUTE_FOUND ``payload`` via ``_on_route_found``, run
    the resolver window, and return the resolved ``(path, eph) | None``."""
    svc = origin.service
    svc._discovery_timeout_s = discovery_timeout_s
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    request_id = str(payload["request_id"])
    svc._pending[request_id] = _PendingDiscovery(
        future=fut,
        target=target_instance_id,
    )
    ev = FederationEvent(
        msg_id="m-rf-attack",
        event_type=FederationEventType.SPACE_ROUTE_FOUND,
        from_instance="relay",
        to_instance=origin.instance_id,
        timestamp="2026-06-09T00:00:00Z",
        payload=payload,
    )
    await svc._on_route_found(ev)
    # Run the collection window (if a response was collected, the first
    # one scheduled the resolver; drive it directly so the test is
    # deterministic regardless of scheduling).
    await svc._resolve_after_window(request_id)
    if fut.done():
        return fut.result()
    return None


async def test_forged_route_found_substituting_attacker_key_is_rejected():
    """REGRESSION (vuln): a confirmed relay forges ROUTE_FOUND for target
    T carrying its OWN ephemeral + its OWN (validly self-signed) identity
    key. Because ``derive_instance_id(attacker_identity) != T``, the
    origin DROPS it — ``discover_route`` resolves to None, NOT the
    attacker's key, so no space content is ever sealed under it."""
    nodes = _build_mesh({"origin": [], "target": [], "attacker": []})
    origin = nodes["origin"]
    target_id = nodes["target"].instance_id
    attacker = nodes["attacker"]

    # Attacker mints its own eph + signs it with its own identity — a
    # perfectly valid self-signature, just for the WRONG identity.
    attacker_eph = attacker.service._generate_target_eph(time.monotonic())
    forged = _signed_route_found_payload(
        request_id="rid-attack",
        path=[origin.instance_id, attacker.instance_id],
        target_eph_pk_b64=attacker_eph,
        signer_seed=attacker.fed.own_identity_seed,
        signer_pk=attacker.fed.own_identity_pk,
    )

    result = await _drive_origin_route_found(
        origin, target_instance_id=target_id, payload=forged
    )
    assert result is None, (
        "origin accepted an attacker-substituted target_eph_pk — relay MITM not closed"
    )
    # And nothing was collected.
    assert origin.service._pending["rid-attack"].responses == []


async def test_legit_signed_route_found_is_accepted():
    """A ROUTE_FOUND genuinely signed by the target identity (whose
    ``derive_instance_id == target``) over THIS request, with
    ``path[-1] == target``, is collected and resolves to
    ``(path, target_eph, target_identity_pk)`` — the pk the origin just
    verified rides along so the cache can pin it."""
    nodes = _build_mesh({"origin": [], "target": []})
    origin = nodes["origin"]
    target = nodes["target"]
    target_id = target.instance_id

    target_eph = target.service._generate_target_eph(time.monotonic())
    legit = _signed_route_found_payload(
        request_id="rid-legit",
        path=[origin.instance_id, target_id],
        target_eph_pk_b64=target_eph,
        signer_seed=target.fed.own_identity_seed,
        signer_pk=target.fed.own_identity_pk,
    )

    result = await _drive_origin_route_found(
        origin, target_instance_id=target_id, payload=legit
    )
    assert result == (
        [origin.instance_id, target_id],
        target_eph,
        target.fed.own_identity_pk.hex(),
    )


async def test_route_found_with_tampered_signature_is_dropped():
    """A response carrying the genuine target identity key but a
    corrupted signature is dropped."""
    nodes = _build_mesh({"origin": [], "target": []})
    origin = nodes["origin"]
    target = nodes["target"]
    target_id = target.instance_id
    target_eph = target.service._generate_target_eph(time.monotonic())
    payload = _signed_route_found_payload(
        request_id="rid-tampered",
        path=[origin.instance_id, target_id],
        target_eph_pk_b64=target_eph,
        signer_seed=target.fed.own_identity_seed,
        signer_pk=target.fed.own_identity_pk,
    )
    # Flip the signature bytes.
    bad = bytearray(b64url_decode(payload["target_eph_sig"]))
    bad[0] ^= 0xFF
    payload["target_eph_sig"] = b64url_encode(bytes(bad))

    result = await _drive_origin_route_found(
        origin, target_instance_id=target_id, payload=payload
    )
    assert result is None


async def test_route_found_signed_by_wrong_key_for_target_is_dropped():
    """A response whose ``target_identity_pk`` hashes to the right
    target id is required; a different (valid, self-consistent) identity
    signing for the target is dropped because its id != target."""
    nodes = _build_mesh({"origin": [], "target": [], "other": []})
    origin = nodes["origin"]
    target_id = nodes["target"].instance_id
    other = nodes["other"]
    other_eph = other.service._generate_target_eph(time.monotonic())
    # 'other' signs correctly for ITS OWN key, claims path ends at target.
    payload = _signed_route_found_payload(
        request_id="rid-wrongkey",
        path=[origin.instance_id, target_id],
        target_eph_pk_b64=other_eph,
        signer_seed=other.fed.own_identity_seed,
        signer_pk=other.fed.own_identity_pk,
    )
    result = await _drive_origin_route_found(
        origin, target_instance_id=target_id, payload=payload
    )
    assert result is None


async def test_route_found_missing_identity_fields_is_dropped():
    """A legacy/sub-v_N ROUTE_FOUND with no ``target_identity_pk`` /
    ``target_eph_sig`` is dropped (fail-closed)."""
    nodes = _build_mesh({"origin": [], "target": []})
    origin = nodes["origin"]
    target_id = nodes["target"].instance_id
    payload = {
        "request_id": "rid-legacy",
        "path": [origin.instance_id, target_id],
        "target_eph_pk": "some-eph-b64",
        # No target_identity_pk / target_eph_sig.
    }
    result = await _drive_origin_route_found(
        origin, target_instance_id=target_id, payload=payload
    )
    assert result is None


async def test_route_found_path_not_ending_at_target_is_dropped():
    """Even a perfectly valid signature over the eph key is dropped when
    ``path[-1] != target`` — the route must actually terminate at the
    asked-for target."""
    nodes = _build_mesh({"origin": [], "target": []})
    origin = nodes["origin"]
    target = nodes["target"]
    target_id = target.instance_id
    target_eph = target.service._generate_target_eph(time.monotonic())
    payload = _signed_route_found_payload(
        request_id="rid-badpath",
        # Genuinely signed by the target, but the path ends elsewhere.
        path=[origin.instance_id, "some-other-hop"],
        target_eph_pk_b64=target_eph,
        signer_seed=target.fed.own_identity_seed,
        signer_pk=target.fed.own_identity_pk,
    )
    result = await _drive_origin_route_found(
        origin, target_instance_id=target_id, payload=payload
    )
    assert result is None


async def test_route_found_malformed_hex_identity_is_dropped():
    """A non-hex ``target_identity_pk`` is dropped without raising."""
    nodes = _build_mesh({"origin": [], "target": []})
    origin = nodes["origin"]
    target_id = nodes["target"].instance_id
    payload = {
        "request_id": "rid-badhex",
        "path": [origin.instance_id, target_id],
        "target_eph_pk": "some-eph-b64",
        "target_identity_pk": "nothex!!",
        "target_eph_sig": "also-bad",
    }
    result = await _drive_origin_route_found(
        origin, target_instance_id=target_id, payload=payload
    )
    assert result is None


# ── #648: the origin must never seal under a key the target has dropped ──


def test_route_cache_ttl_is_strictly_inside_target_eph_ttl():
    """The origin's cache window MUST close before the target's key does.

    The origin seals every routed payload under the ``target_eph_pk`` its
    route cache holds, but the matching private half lives in the
    *target's* memory on its own timer. If the origin's window can outlive
    the target's, the origin keeps sealing under a dead key and the target
    discards every envelope in silence — that is #648.

    Asserted as the invariant rather than against the literals so the
    two constants can be retuned together without the test going stale,
    but cannot drift apart.
    """
    assert ROUTE_CACHE_TTL_S < routed_crypto.DEFAULT_TARGET_EPH_TTL_S
    assert (
        routed_crypto.DEFAULT_TARGET_EPH_TTL_S - ROUTE_CACHE_TTL_S
        == ROUTE_CACHE_SAFETY_MARGIN_S
    )
    # The default a service is constructed with must be the derived value,
    # not an independently-typed 300.0 that happens to match today.
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    default_svc = RouteDiscoveryService(
        federation_service=nodes["a"].fed,  # type: ignore[arg-type]
        federation_repo=nodes["a"].repo,  # type: ignore[arg-type]
    )
    assert default_svc._cache_ttl_s == ROUTE_CACHE_TTL_S
    assert default_svc._cache_ttl_s < default_svc._target_eph_ttl_s


async def test_cache_expiry_anchored_on_probe_start_not_resolution():
    """``expires_at`` is measured from when the probe was SENT.

    The target minted its ephemeral strictly *after* our probe left, so
    anchoring on resolution would hand the origin a window longer than the
    target's by the discovery latency — the exact overhang that lets the
    origin outlive the key.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]}, discovery_timeout_s=0.05)
    b_id = nodes["b"].instance_id
    svc = nodes["a"].service

    before = time.monotonic()
    assert await svc.discover_route(b_id) is not None
    after = time.monotonic()

    cached = svc._route_cache[b_id]
    # Recover the instant the window was anchored on.
    anchored_at = cached.expires_at - svc._cache_ttl_s
    # It cannot predate our entry into discovery...
    assert anchored_at >= before
    # ...and it must sit at the START of the probe, not at resolution:
    # the discovery here burns a full ``discovery_timeout_s`` collection
    # window, so resolution-anchoring would land ~that much later.
    elapsed = after - before
    assert elapsed > 0.02, "discovery resolved too fast to distinguish"
    assert anchored_at - before < elapsed / 2, (
        "expiry looks anchored on resolution, not on probe start"
    )


async def test_concurrent_discovery_for_same_target_single_flights():
    """Two concurrent discoveries for one target share a single flood.

    ``_send`` re-consults discovery per chunk, so a stream that starts
    re-discovering (or a retry loop) would otherwise spray every
    mesh-capable peer once per chunk — O(chunks x peers) signed events.
    """
    nodes = _build_mesh({"a": ["b", "c"], "b": ["a", "d"], "c": ["a"], "d": ["b"]})
    d_id = nodes["d"].instance_id
    svc = nodes["a"].service
    probes_before = sum(
        1
        for s in nodes["a"].fed.sent
        if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
    )

    first, second = await asyncio.gather(
        svc.discover_route(d_id),
        svc.discover_route(d_id),
    )

    assert first is not None
    # Both callers get the same answer, and only one flood went out.
    assert first == second
    probes = [
        s
        for s in nodes["a"].fed.sent
        if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
    ]
    request_ids = {s["payload"]["request_id"] for s in probes}
    assert len(probes) - probes_before > 0
    assert len(request_ids) == 1, "second caller started its own flood"


async def test_negative_result_puts_target_in_cooldown():
    """A discovery that found nothing isn't immediately re-flooded."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]}, discovery_timeout_s=0.01)
    svc = nodes["a"].service
    unknown = "z" * 52

    assert await svc.discover_route(unknown) is None
    after_first = len(
        [
            s
            for s in nodes["a"].fed.sent
            if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
        ]
    )
    assert unknown in svc._negative_until

    # Second attempt inside the cooldown must not put a probe on the wire.
    assert await svc.discover_route(unknown) is None
    after_second = len(
        [
            s
            for s in nodes["a"].fed.sent
            if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
        ]
    )
    assert after_second == after_first


async def test_invalidate_clears_the_negative_cooldown():
    """``invalidate`` is an explicit "look again" — it must lift the cooldown.

    Otherwise the forced re-discovery that ``send_with_mesh_fallback``
    performs after a failed routed ship gets swallowed by the cooldown the
    preceding failure installed, and the retry is a silent no-op.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]}, discovery_timeout_s=0.01)
    svc = nodes["a"].service
    unknown = "z" * 52

    assert await svc.discover_route(unknown) is None
    assert unknown in svc._negative_until

    await svc.invalidate(unknown)
    assert unknown not in svc._negative_until

    probes_before = len(
        [
            s
            for s in nodes["a"].fed.sent
            if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
        ]
    )
    await svc.discover_route(unknown)
    probes_after = len(
        [
            s
            for s in nodes["a"].fed.sent
            if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
        ]
    )
    assert probes_after > probes_before, "cooldown still suppressed the re-probe"


async def test_lookup_target_eph_priv_does_not_extend_on_use():
    """Using a target ephemeral must NOT slide its expiry forward.

    Tempting one-line "fix" for #648 and deliberately rejected: the
    documented bound is mint + TTL, and refreshing on use would turn "no
    forward secrecy beyond the discovery window" into "none while traffic
    flows" (``docs/crypto.md``). The fix is re-discovery, which rotates
    the key — the FS-positive direction.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc = nodes["a"].service
    pub = svc._generate_target_eph(time.monotonic())
    _priv, expiry_at_mint = svc._target_eph_state[pub]

    assert svc.lookup_target_eph_priv(pub) is not None
    _priv2, expiry_after_use = svc._target_eph_state[pub]
    assert expiry_after_use == expiry_at_mint


async def test_prune_expired_bounds_the_negative_cooldown_dict():
    """The cooldown map is pruned like every other TTL dict.

    Each failed discovery adds an entry, so a caller asking us to route
    to a stream of unknown targets would otherwise grow it without
    bound — the same audit finding the sibling dicts already carry a cap
    for.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]}, discovery_timeout_s=0.01)
    svc = nodes["a"].service

    svc._negative_until = {
        "stale-target": time.monotonic() - 1.0,
        "live-target": time.monotonic() + 60.0,
    }
    svc._prune_expired(time.monotonic())

    assert "stale-target" not in svc._negative_until
    assert "live-target" in svc._negative_until


# ── #648: a ROUTE_FOUND that arrives late is still worth having ───────


async def test_late_route_found_warms_the_cache():
    """An answer that lands after the hard cap is cached, not discarded.

    ``_pending`` is popped the instant the wait resolves, so a
    ROUTE_FOUND arriving after ``discovery_timeout_s * 2`` used to fall
    through to the relay branch, find no cached caller, and vanish. That
    is precisely what a just-rebooted peer produces — its first round trip
    pays a DTLS handshake plus a STUN probe — leaving the origin with no
    route while a perfectly good answer sat in its inbox, and the chunk
    stream abandoned after three instant failures.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]}, discovery_timeout_s=0.01)
    origin = nodes["a"].service
    target_id = nodes["b"].instance_id

    # Drive a discovery that is guaranteed to time out: the probes go
    # nowhere, so nothing answers inside the window.
    nodes["a"].fed.peers.clear()
    assert await origin.discover_route(target_id) is None
    assert target_id not in origin._route_cache

    # The request must still be attributable after the wait gave up.
    req_ids = list(origin._origin_requests)
    assert req_ids, "origin forgot what it asked for"
    request_id = req_ids[0]
    assert origin._origin_requests[request_id][0] == target_id

    # Now the answer finally lands, correctly signed by the target.
    eph_pk = nodes["b"].service._generate_target_eph(time.monotonic())
    payload = _signed_route_found_payload(
        request_id=request_id,
        path=[nodes["a"].instance_id, target_id],
        target_eph_pk_b64=eph_pk,
        signer_seed=nodes["b"].fed.own_identity_seed,
        signer_pk=nodes["b"].fed.own_identity_pk,
    )
    await origin._on_route_found(
        FederationEvent(
            msg_id="late-1",
            event_type=FederationEventType.SPACE_ROUTE_FOUND,
            from_instance=target_id,
            to_instance=nodes["a"].instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=payload,
        )
    )

    cached = origin._route_cache.get(target_id)
    assert cached is not None, "late ROUTE_FOUND was thrown away"
    assert cached.target_eph_pk == payload["target_eph_pk"]
    # And the cooldown the timeout installed is lifted, so the next call
    # resolves from cache rather than being suppressed.
    assert target_id not in origin._negative_until
    assert await origin.discover_route(target_id) == (
        [nodes["a"].instance_id, target_id],
        payload["target_eph_pk"],
    )


async def test_late_route_found_still_verifies_the_signature():
    """The late path must not become a hole in the v_21 binding.

    A confirmed relay that forges a ROUTE_FOUND with its own ephemeral
    key would hand itself our plaintext. Arriving late is not a reason to
    skip the check.
    """
    nodes = _build_mesh(
        {"a": ["b"], "b": ["a"], "c": ["a"]},
        discovery_timeout_s=0.01,
    )
    origin = nodes["a"].service
    target_id = nodes["b"].instance_id
    nodes["a"].fed.peers.clear()
    assert await origin.discover_route(target_id) is None
    request_id = next(iter(origin._origin_requests))

    # Signed by C, but claiming a path that ends at B.
    eph_pk = nodes["c"].service._generate_target_eph(time.monotonic())
    forged = _signed_route_found_payload(
        request_id=request_id,
        path=[nodes["a"].instance_id, target_id],
        target_eph_pk_b64=eph_pk,
        signer_seed=nodes["c"].fed.own_identity_seed,
        signer_pk=nodes["c"].fed.own_identity_pk,
    )
    await origin._on_route_found(
        FederationEvent(
            msg_id="late-forged",
            event_type=FederationEventType.SPACE_ROUTE_FOUND,
            from_instance=target_id,
            to_instance=nodes["a"].instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=forged,
        )
    )

    assert target_id not in origin._route_cache


async def test_origin_requests_are_pruned():
    """The attribution map is bounded like every other TTL dict."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc = nodes["a"].service
    svc._origin_requests = {
        "stale": ("target-x", time.monotonic() - 1.0),
        "live": ("target-y", time.monotonic() + 60.0),
    }
    svc._prune_expired(time.monotonic())
    assert "stale" not in svc._origin_requests
    assert "live" in svc._origin_requests


async def test_invalidate_if_older_than_keeps_a_freshly_minted_route():
    """A route minted after the cutoff survives; an older one doesn't.

    The host drops its cached route when a mesh requester sends BEGIN,
    because that route may predate the requester's restart. But once the
    requester retries, invalidating unconditionally would throw away the
    route re-discovered for the previous BEGIN — including one warmed by a
    late ROUTE_FOUND — and re-probe into the same timeout forever (#648).
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc = nodes["a"].service
    target = nodes["b"].instance_id

    assert await svc.discover_route(target) is not None
    minted_at = svc._route_cache[target].expires_at - svc._cache_ttl_s

    # Cutoff before the route was minted → it is current, keep it.
    assert await svc.invalidate_if_older_than(target, cutoff=minted_at - 1.0) is False
    assert target in svc._route_cache

    # Cutoff after it was minted → it predates the event, drop it.
    assert await svc.invalidate_if_older_than(target, cutoff=minted_at + 1.0) is True
    assert target not in svc._route_cache


async def test_invalidate_if_older_than_is_a_noop_without_a_cached_route():
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc = nodes["a"].service
    assert (
        await svc.invalidate_if_older_than("nobody", cutoff=time.monotonic()) is False
    )


async def test_invalidate_if_older_than_lifts_the_cooldown_when_it_drops():
    """Dropping a stale route must also clear its negative cooldown."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc = nodes["a"].service
    target = nodes["b"].instance_id
    assert await svc.discover_route(target) is not None
    svc._negative_until[target] = time.monotonic() + 60.0

    minted_at = svc._route_cache[target].expires_at - svc._cache_ttl_s
    assert await svc.invalidate_if_older_than(target, cutoff=minted_at + 1.0) is True
    assert target not in svc._negative_until


async def test_invalidate_if_eph_drops_only_the_matching_key():
    """The origin's SPACE_ROUTE_STALE handler invalidates conditionally: a
    nack names the eph it was sealed under, and only a cache entry still
    pointing at THAT key is stale. A route already rebuilt under a fresh
    key survives a late nack for the old one (no re-flood)."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc = nodes["a"].service
    target = nodes["b"].instance_id

    assert await svc.discover_route(target) is not None
    live_eph = svc._route_cache[target].target_eph_pk
    _priv, other_eph = routed_crypto.generate_ephemeral_keypair()

    # Names a key the cache does not point at → keep, report False.
    assert await svc.invalidate_if_eph(target, target_eph_pk=other_eph) is False
    assert target in svc._route_cache

    # Names the live key → drop, report True.
    assert await svc.invalidate_if_eph(target, target_eph_pk=live_eph) is True
    assert target not in svc._route_cache


async def test_invalidate_if_eph_is_a_noop_without_a_cached_route():
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc = nodes["a"].service
    assert await svc.invalidate_if_eph("nobody", target_eph_pk="any") is False


async def test_invalidate_if_eph_lifts_the_cooldown_when_it_drops():
    """Same contract as ``invalidate`` / ``invalidate_if_older_than``: a drop
    is an explicit "go look again", so the negative cooldown goes with it —
    and a keep leaves it alone."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    svc = nodes["a"].service
    target = nodes["b"].instance_id
    assert await svc.discover_route(target) is not None
    live_eph = svc._route_cache[target].target_eph_pk
    svc._negative_until[target] = time.monotonic() + 60.0

    assert await svc.invalidate_if_eph(target, target_eph_pk="stale") is False
    assert target in svc._negative_until
    assert await svc.invalidate_if_eph(target, target_eph_pk=live_eph) is True
    assert target not in svc._negative_until


async def test_cooldown_remaining_exposes_the_negative_window():
    """Callers need to tell "we didn't probe" apart from "we probed and
    failed".

    ``discover_route`` returns ``None`` for both, and a caller in a chunk
    loop that treats the two the same burns its whole retry budget in
    milliseconds against a cooldown that is about to lift (#664 follow-up).
    ``cooldown_remaining`` is the accessor that makes the difference
    visible — and asking for it must never arm or extend the cooldown.
    """
    nodes = _build_mesh({"a": ["b"], "b": ["a"]}, discovery_timeout_s=0.01)
    svc = nodes["a"].service
    unknown = "z" * 52

    assert svc.cooldown_remaining(unknown) == 0.0

    assert await svc.discover_route(unknown) is None
    remaining = svc.cooldown_remaining(unknown)
    assert 0.0 < remaining <= route_discovery.ROUTE_NEGATIVE_COOLDOWN_S

    # Reading it neither probes nor re-arms — the anti-flood property holds.
    probes_before = len(
        [
            s
            for s in nodes["a"].fed.sent
            if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
        ]
    )
    assert svc.cooldown_remaining(unknown) <= remaining
    assert await svc.discover_route(unknown) is None
    assert (
        len(
            [
                s
                for s in nodes["a"].fed.sent
                if s["event_type"] is FederationEventType.SPACE_FIND_ROUTE
            ]
        )
        == probes_before
    )

    # An expired window reads as zero.
    svc._negative_until[unknown] = time.monotonic() - 1.0
    assert svc.cooldown_remaining(unknown) == 0.0


# ── Target identity pk pinned at discovery (route-stale nack, T3) ──────


async def test_cached_route_pins_the_verified_target_identity_pk():
    """The ``_CachedRoute`` written by a normal resolve carries the identity
    pk the origin verified in ``_verify_route_found`` — the key a later
    ``SPACE_ROUTE_STALE`` nack is checked against — and the pure accessor
    exposes it."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    a, b = nodes["a"], nodes["b"]
    assert await a.service.discover_route(b.instance_id) is not None
    cached = a.service._route_cache[b.instance_id]
    assert cached.target_identity_pk == b.fed.own_identity_pk.hex()
    assert (
        a.service.cached_target_identity_pk(b.instance_id)
        == b.fed.own_identity_pk.hex()
    )


async def test_cached_target_identity_pk_is_none_without_a_live_entry():
    """``None`` when nothing is cached, after ``invalidate``, and once the
    entry has expired — and asking never mutates the cache."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]})
    a, b = nodes["a"], nodes["b"]
    assert a.service.cached_target_identity_pk(b.instance_id) is None
    assert await a.service.discover_route(b.instance_id) is not None
    assert a.service.cached_target_identity_pk(b.instance_id) is not None
    await a.service.invalidate(b.instance_id)
    assert a.service.cached_target_identity_pk(b.instance_id) is None
    # Expired-but-not-yet-pruned entry reads as absent, and stays in place
    # (the accessor is a pure read — pruning is ``_prune_expired``'s job).
    a.service._route_cache[b.instance_id] = _CachedRoute(
        path=[a.instance_id, b.instance_id],
        target_eph_pk="eph",
        target_identity_pk="pk",
        expires_at=time.monotonic() - 1.0,
    )
    assert a.service.cached_target_identity_pk(b.instance_id) is None
    assert b.instance_id in a.service._route_cache


async def test_local_target_pins_own_identity_pk():
    nodes = _build_mesh({"a": []})
    a = nodes["a"]
    assert await a.service.discover_route(a.instance_id) is not None
    assert (
        a.service.cached_target_identity_pk(a.instance_id)
        == a.fed.own_identity_pk.hex()
    )


async def test_resolve_after_window_propagates_the_identity_pk_of_the_pick():
    """The random shortest-path pick carries its own identity pk through
    to the future — every candidate here has the same (verified) target,
    so the pk is the same for all, but it must be the one that travelled
    with the picked response, not a default."""
    nodes = _build_mesh({"a": []}, discovery_timeout_s=0.01)
    svc_a = nodes["a"].service
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[tuple[list[str], str, str] | None] = loop.create_future()
    pending = _PendingDiscovery(future=fut, target="t")
    pending.responses.append((["a", "x", "t"], "eph-long", "pk-t"))
    pending.responses.append((["a", "t"], "eph-short", "pk-t"))
    svc_a._pending["rid-pk"] = pending
    await svc_a._resolve_after_window("rid-pk")
    assert fut.result() == (["a", "t"], "eph-short", "pk-t")


async def test_late_route_found_pins_the_identity_pk_too():
    """The late-answer path caches through the same ``_cache_route`` and
    must pin the pk as well — otherwise a nack for a route learned late
    would find an empty pin."""
    nodes = _build_mesh({"a": ["b"], "b": ["a"]}, discovery_timeout_s=0.01)
    origin = nodes["a"].service
    target_id = nodes["b"].instance_id
    nodes["a"].fed.peers.clear()
    assert await origin.discover_route(target_id) is None
    request_id = list(origin._origin_requests)[0]
    eph_pk = nodes["b"].service._generate_target_eph(time.monotonic())
    payload = _signed_route_found_payload(
        request_id=request_id,
        path=[nodes["a"].instance_id, target_id],
        target_eph_pk_b64=eph_pk,
        signer_seed=nodes["b"].fed.own_identity_seed,
        signer_pk=nodes["b"].fed.own_identity_pk,
    )
    await origin._on_route_found(
        FederationEvent(
            msg_id="late-pk",
            event_type=FederationEventType.SPACE_ROUTE_FOUND,
            from_instance=target_id,
            to_instance=nodes["a"].instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=payload,
        )
    )
    assert (
        origin.cached_target_identity_pk(target_id)
        == nodes["b"].fed.own_identity_pk.hex()
    )

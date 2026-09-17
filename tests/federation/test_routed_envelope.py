"""Tests for :class:`SpaceRoutedHandler` (§D2 PR 2 / v_6 mesh).

Covers:

* Send-then-relay-then-unwrap: an envelope shipped along [a, b, c]
  emerges as a synthesised :class:`FederationEvent` at c, with
  ``from_instance == a`` and ``routed_path == [a, b, c]``. The
  relay sees only the sealed ciphertext, never the plaintext.
* Reply leg: target ships an ACK via :meth:`send_routed_reply`; the
  origin unseals it and dispatches the inner event.
* Loop prevention: a relay drops an envelope whose ``route_id`` it
  has already seen.
* Cycle detection: an envelope whose forward path contains self
  beyond ``position+1`` is dropped.
* Wrong-next-hop: an envelope where ``path[position+1] != self`` is
  dropped (defensive).
* Missing target_eph_priv at unwrap → log + drop.
* Unknown direction → log + drop.
* send_routed validation: empty path / wrong origin raises.
* Defensive drops: missing route_id / path / position / non-dict
  sealed all dropped silently.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from socialhome.crypto import (
    Ed25519Keypair,
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
    PairingStatus,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation import routed_crypto
from socialhome.federation.route_discovery import RouteDiscoveryService
from socialhome.federation.routed_envelope import (
    _MAX_PENDING_ROUTED,
    _MAX_RETAINED_INNER_BYTES,
    _MAX_ROUTED_PATH_LEN,
    SpaceRoutedHandler,
    _PendingRouted,
)


# ── Test doubles ──────────────────────────────────────────────────────


@dataclass
class _FakeInstance:
    id: str
    status: PairingStatus = PairingStatus.CONFIRMED
    proto_version: int = 6


class _FakeFederationRepo:
    def __init__(self, instances: dict[str, _FakeInstance] | None = None) -> None:
        self._instances = instances or {}

    async def get_instance(self, instance_id: str) -> _FakeInstance | None:
        return self._instances.get(instance_id)


class _LinkedFederationService:
    """In-memory link between nodes — ``send_event`` ships straight
    into the destination node's :class:`SpaceRoutedHandler`.
    """

    def __init__(
        self,
        *,
        own_instance_id: str,
        identity: Ed25519Keypair | None = None,
    ) -> None:
        self._own_instance_id = own_instance_id
        self.identity = identity or generate_identity_keypair()
        self.sent: list[dict[str, Any]] = []
        self.peers: dict[str, SpaceRoutedHandler] = {}
        #: ``instance_id → proto_version`` consulted by ``peer_supports``;
        #: unknown peers read as ``default_peer_version`` (current wire).
        self.peer_versions: dict[str, int] = {}
        self.default_peer_version: int = FederationCapability.MIN_FOR_ROUTE_STALE_NACK

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    @property
    def own_identity_seed(self) -> bytes:
        return self.identity.private_key

    @property
    def own_identity_pk(self) -> bytes:
        return self.identity.public_key

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        version = self.peer_versions.get(instance_id, self.default_peer_version)
        return version >= min_version

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
        dest = self.peers.get(to_instance_id)
        if dest is None or event_type not in (
            FederationEventType.SPACE_ROUTED,
            FederationEventType.SPACE_ROUTE_STALE,
        ):
            # An unwired peer means "don't deliver in this test", not
            # "delivery failed": the transport accepted the envelope.
            # Tests that want ``ok=False`` patch ``send_event`` directly.
            return SimpleNamespace(ok=True, instance_id=to_instance_id)
        ev = FederationEvent(
            msg_id=f"m-{len(self.sent)}",
            event_type=event_type,
            from_instance=self._own_instance_id,
            to_instance=to_instance_id,
            timestamp="2026-05-22T00:00:00Z",
            payload=payload,
        )

        # Fire-and-forget to mirror real transport.
        async def _deliver() -> None:
            if event_type is FederationEventType.SPACE_ROUTE_STALE:
                await dest._on_route_stale(ev)
            else:
                await dest._on_routed(ev)

        asyncio.create_task(_deliver())
        return SimpleNamespace(ok=True, instance_id=to_instance_id)


@dataclass
class _Node:
    instance_id: str
    fed: _LinkedFederationService
    repo: _FakeFederationRepo
    handler: SpaceRoutedHandler
    dispatched: list[FederationEvent]
    #: ``target_eph_pk_b64 → target_eph_priv_b64`` — populated by the
    #: test driver when the test mints an ephemeral *for* this node;
    #: the handler's ``target_eph_lookup`` reads it on unwrap.
    target_eph_store: dict[str, str]


def _build_chain(
    node_ids: list[str],
    *,
    identities: dict[str, Ed25519Keypair] | None = None,
) -> dict[str, _Node]:
    """Build a chain of nodes wired against each other.

    Each node's :class:`SpaceRoutedHandler` ships to other nodes'
    handlers via ``_LinkedFederationService``. The handler's
    ``target_eph_lookup`` reads from each node's ``target_eph_store``
    — the test mints an ephemeral for the target node and seeds the
    pub → priv mapping before shipping ``send_routed``.
    """
    nodes: dict[str, _Node] = {}
    for node_id in node_ids:
        dispatched: list[FederationEvent] = []
        target_eph_store: dict[str, str] = {}

        async def _dispatcher(
            ev: FederationEvent,
            captured: list[FederationEvent] = dispatched,
        ) -> None:
            captured.append(ev)

        def _lookup(pub: str, store: dict[str, str] = target_eph_store) -> str | None:
            return store.get(pub)

        fed = _LinkedFederationService(
            own_instance_id=node_id,
            identity=(identities or {}).get(node_id),
        )
        repo = _FakeFederationRepo()
        handler = SpaceRoutedHandler(
            federation_service=fed,  # type: ignore[arg-type]
            federation_repo=repo,  # type: ignore[arg-type]
            event_dispatcher=_dispatcher,
            target_eph_lookup=_lookup,
        )
        nodes[node_id] = _Node(
            instance_id=node_id,
            fed=fed,
            repo=repo,
            handler=handler,
            dispatched=dispatched,
            target_eph_store=target_eph_store,
        )
    # Wire peers.
    for node_id, node in nodes.items():
        for other_id, other in nodes.items():
            if other_id == node_id:
                continue
            node.fed.peers[other_id] = other.handler
            node.repo._instances[other_id] = _FakeInstance(id=other_id)
    return nodes


def _build_identity_chain(n: int) -> list[_Node]:
    """Like :func:`_build_chain` but every node's instance id is
    *derived* from a freshly minted Ed25519 identity — the shape the
    ``SPACE_ROUTE_STALE`` relay check ``derive_instance_id(pk) ==
    path[-1]`` needs. Returned in path order (origin first)."""
    identities = [generate_identity_keypair() for _ in range(n)]
    ids = [derive_instance_id(kp.public_key) for kp in identities]
    nodes = _build_chain(ids, identities=dict(zip(ids, identities, strict=True)))
    return [nodes[i] for i in ids]


def _mint_target_eph(node: _Node) -> str:
    """Mint a fresh ephemeral keypair for ``node`` (as if its
    :class:`RouteDiscoveryService` had answered a FIND_ROUTE) and
    seed the priv into its target_eph_store. Returns the pub b64."""
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    node.target_eph_store[pub] = priv
    return pub


# ── Tests ─────────────────────────────────────────────────────────────


async def test_send_relay_unwrap_dispatches_with_origin_attribution():
    """A SPACE_ROUTED envelope shipped from a along [a, b, c] is
    relayed by b (without seeing plaintext) and unwrapped at c — c's
    dispatcher sees the inner event with ``from_instance == 'a'``
    (the origin), ``routed_path == [a, b, c]``, and
    ``routed_route_id`` set so the reply leg can correlate.
    """
    nodes = _build_chain(["a", "b", "c"])
    target_eph_pk = _mint_target_eph(nodes["c"])
    inner_payload = {"redeem_nonce": "n1", "invite_token": "tkn"}
    route_id = await nodes["a"].handler.send_routed(
        path=["a", "b", "c"],
        target_eph_pk_b64=target_eph_pk,
        inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        inner_payload=inner_payload,
    )
    assert route_id
    # Yield to the asyncio loop so the chained deliveries fire.
    for _ in range(8):
        await asyncio.sleep(0)
    # a shipped to b
    a_to_b = [s for s in nodes["a"].fed.sent if s["to"] == "b"]
    assert len(a_to_b) == 1
    # The relay never sees plaintext: the on-wire ``sealed`` blob
    # bears no resemblance to the original payload.
    a_payload = a_to_b[0]["payload"]
    assert "sealed" in a_payload
    assert "inner_payload" not in a_payload  # no plaintext leak
    sealed_json = json.dumps(a_payload["sealed"], sort_keys=True)
    assert "redeem_nonce" not in sealed_json
    assert "tkn" not in sealed_json
    # b shipped to c (position bumped from 0 to 1), propagating the
    # sealed blob unchanged.
    fwds_from_b = [s for s in nodes["b"].fed.sent if s["to"] == "c"]
    assert len(fwds_from_b) == 1
    assert fwds_from_b[0]["payload"]["position"] == 1
    assert fwds_from_b[0]["payload"]["direction"] == "forward"
    assert fwds_from_b[0]["payload"]["sealed"] == a_payload["sealed"]
    # c dispatched the inner event with origin attribution.
    assert len(nodes["c"].dispatched) == 1
    inner = nodes["c"].dispatched[0]
    assert inner.event_type is FederationEventType.SPACE_INVITE_TOKEN_REDEEM
    assert inner.from_instance == "a"
    assert inner.to_instance == "c"
    assert inner.payload == inner_payload
    assert inner.routed_path == ["a", "b", "c"]
    assert inner.routed_route_id == route_id


async def test_reply_leg_seals_back_to_origin():
    """After the forward leg lands at the target, the target ships a
    reply via :meth:`send_routed_reply` — the origin unseals it and
    dispatches the inner ACK event. The relay still only sees
    ciphertext."""
    nodes = _build_chain(["a", "b", "c"])
    target_eph_pk = _mint_target_eph(nodes["c"])
    await nodes["a"].handler.send_routed(
        path=["a", "b", "c"],
        target_eph_pk_b64=target_eph_pk,
        inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        inner_payload={"redeem_nonce": "n1", "invite_token": "tkn"},
    )
    for _ in range(8):
        await asyncio.sleep(0)
    forward = nodes["c"].dispatched[0]
    route_id = forward.routed_route_id
    assert route_id is not None
    # Target ships the ACK back via send_routed_reply.
    ack_payload = {"redeem_nonce": "n1", "space_id": "sp-1", "role": "member"}
    await nodes["c"].handler.send_routed_reply(
        route_id=route_id,
        inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        inner_payload=ack_payload,
    )
    for _ in range(8):
        await asyncio.sleep(0)
    # c shipped a SPACE_ROUTED to b with direction=reply.
    c_to_b = [
        s
        for s in nodes["c"].fed.sent
        if s["to"] == "b" and s["event_type"] is FederationEventType.SPACE_ROUTED
    ]
    assert len(c_to_b) == 1
    assert c_to_b[0]["payload"]["direction"] == "reply"
    assert c_to_b[0]["payload"]["path"] == ["c", "b", "a"]
    # Sealed blob doesn't leak the ack plaintext.
    sealed_json = json.dumps(c_to_b[0]["payload"]["sealed"], sort_keys=True)
    assert "sp-1" not in sealed_json
    assert "member" not in sealed_json
    # a dispatched the unsealed ACK.
    ack_events = [
        e
        for e in nodes["a"].dispatched
        if e.event_type is FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK
    ]
    assert len(ack_events) == 1
    assert ack_events[0].payload == ack_payload
    assert ack_events[0].from_instance == "c"
    assert ack_events[0].routed_path == ["c", "b", "a"]


async def test_send_routed_reply_without_state_raises():
    """A reply for an unknown route_id (never received a forward leg
    or state expired) raises :class:`LookupError`."""
    nodes = _build_chain(["a"])
    with pytest.raises(LookupError):
        await nodes["a"].handler.send_routed_reply(
            route_id="never-seen",
            inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
            inner_payload={},
        )


async def test_loop_detection_drops_duplicate_route_id():
    """A relay that has already seen ``route_id`` drops the inbound."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    handler_b._seen_routes["rid-dupe"] = float("inf")
    nodes["b"].fed.sent.clear()
    ev = FederationEvent(
        msg_id="m-dupe",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-dupe",
            "path": ["a", "b", "x"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []


async def test_cycle_detection_drops_self_in_forward_path():
    """An envelope whose forward portion of ``path`` (after
    ``position+1``) contains self → drop. Discovery should never
    produce this, but defensively the relay catches it."""
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    ev = FederationEvent(
        msg_id="m-cycle",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-cycle",
            "path": ["a", "b", "c", "b"],  # b reappears after position+1
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []


async def test_wrong_next_hop_is_dropped():
    """An envelope where ``path[position+1] != self`` is dropped —
    we're not actually the next hop, so forwarding would be incorrect.
    """
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    ev = FederationEvent(
        msg_id="m-wrong",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            # path[position+1] should be 'b' to reach us; we put 'x'.
            "route_id": "rid-wrong",
            "path": ["a", "x", "c"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []


async def test_overlong_path_is_dropped():
    """A SPACE_ROUTED ``path`` longer than the hard cap is dropped before
    forwarding — a relay trusts the path it's handed, so an over-long chain
    (beyond what discovery can produce) must not be re-fanned."""
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    nodes["b"].dispatched.clear()
    # 9 distinct nodes > _MAX_ROUTED_PATH_LEN (8); we're at position 0.
    long_path = ["a", "b"] + [f"n{i}" for i in range(7)]
    ev = FederationEvent(
        msg_id="m-long",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-long",
            "path": long_path,
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []


async def test_path_position_sender_mismatch_is_dropped():
    """The previous hop named at ``path[position]`` must be the authenticated
    sender; a mismatch (mis-route / off-path replay) is dropped, not forwarded.
    """
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    nodes["b"].dispatched.clear()
    ev = FederationEvent(
        msg_id="m-mismatch",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="z",  # NOT path[position]=='a'
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-mismatch",
            "path": ["a", "b", "c"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []


async def test_send_routed_rejects_short_path():
    """`send_routed` raises on a one-element path — not a route."""
    nodes = _build_chain(["a"])
    pub = _mint_target_eph(nodes["a"])
    with pytest.raises(ValueError, match="at least origin and target"):
        await nodes["a"].handler.send_routed(
            path=["a"],
            target_eph_pk_b64=pub,
            inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            inner_payload={},
        )


async def test_send_routed_rejects_path_with_wrong_origin():
    """`send_routed` raises when ``path[0]`` is not our own instance."""
    nodes = _build_chain(["a", "b"])
    pub = _mint_target_eph(nodes["b"])
    with pytest.raises(ValueError, match="path\\[0\\]"):
        await nodes["a"].handler.send_routed(
            path=["x", "b"],
            target_eph_pk_b64=pub,
            inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            inner_payload={},
        )


async def test_missing_route_id_is_dropped():
    """Envelope lacking ``route_id`` is dropped silently."""
    nodes = _build_chain(["b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-no-rid",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "path": ["a", "b"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_missing_path_is_dropped():
    """Envelope lacking ``path`` is dropped silently."""
    nodes = _build_chain(["b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-no-path",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r1",
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_non_dict_sealed_is_dropped():
    """Envelope with a non-dict ``sealed`` blob is dropped."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-bad-sealed",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r1",
            "path": ["a", "b"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": "not a dict",
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_unknown_inner_event_type_is_dropped():
    """An envelope wrapping an unknown event_type is dropped — we
    can't synthesise a :class:`FederationEvent` for a value not in
    the enum.
    """
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-unknown",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r1",
            "path": ["a", "b"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "future_event_we_dont_know",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_unknown_direction_is_dropped():
    """An envelope with ``direction`` outside the {forward, reply}
    set is dropped — a future direction must arrive in a build that
    understands it, never on a build that would just guess."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-bad-dir",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r-bad-dir",
            "path": ["a", "b"],
            "position": 0,
            "direction": "sideways",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_missing_target_eph_priv_at_unwrap_drops():
    """When the inbound forward leg lands on the target but no cached
    target ephemeral priv matches ``sealed.target_eph_pk`` (expired /
    unknown), the unwrap step drops + logs rather than crashing."""
    nodes = _build_chain(["a", "b"])
    # NOTE: we deliberately do NOT seed nodes["b"].target_eph_store
    # — the lookup returns None and the unwrap should drop.
    bogus_pub = _mint_target_eph(nodes["a"])  # b's store stays empty
    # Use the real seal machinery so JSON parse + unseal logic flows
    # through, but with the wrong target so the lookup fails first.
    inner_payload_json = '{"x":1}'
    o_priv, o_pub = routed_crypto.generate_ephemeral_keypair()
    sealed = routed_crypto.seal_inner_payload(
        inner_payload_json=inner_payload_json,
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=bogus_pub,
        route_id="r-missing",
        inner_event_type="space_invite_token_redeem",
    )
    ev = FederationEvent(
        msg_id="m-miss",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r-missing",
            "path": ["a", "b"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": sealed,
        },
    )
    await nodes["b"].handler._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_attach_to_registers_handler():
    """`attach_to` registers the SPACE_ROUTED and SPACE_ROUTE_STALE handlers."""

    class _FakeRegistry:
        def __init__(self) -> None:
            self.bindings: dict[FederationEventType, list] = {}

        def register(self, et, handler):
            self.bindings.setdefault(et, []).append(handler)

    class _FakeFedSvc:
        def __init__(self) -> None:
            self._event_registry = _FakeRegistry()

        @property
        def own_instance_id(self) -> str:
            return "me"

    fed_svc = _FakeFedSvc()
    repo = _FakeFederationRepo()

    async def _noop(ev: FederationEvent) -> None:
        pass

    handler = SpaceRoutedHandler(
        federation_service=fed_svc,  # type: ignore[arg-type]
        federation_repo=repo,  # type: ignore[arg-type]
        event_dispatcher=_noop,
        target_eph_lookup=lambda _p: None,
    )
    handler.attach_to(fed_svc)  # type: ignore[arg-type]
    assert FederationEventType.SPACE_ROUTED in fed_svc._event_registry.bindings
    assert FederationEventType.SPACE_ROUTE_STALE in fed_svc._event_registry.bindings
    assert fed_svc._event_registry.bindings[FederationEventType.SPACE_ROUTE_STALE] == [
        handler._on_route_stale
    ]


async def test_two_hop_direct_unwrap_at_target():
    """The minimal mesh case: a → b with path=[a, b], position=0.
    b is the target (next_index == len(path)-1), so b unwraps
    immediately rather than forwarding."""
    nodes = _build_chain(["a", "b"])
    target_eph_pk = _mint_target_eph(nodes["b"])
    inner_payload = {"x": 1}
    await nodes["a"].handler.send_routed(
        path=["a", "b"],
        target_eph_pk_b64=target_eph_pk,
        inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        inner_payload=inner_payload,
    )
    for _ in range(4):
        await asyncio.sleep(0)
    assert len(nodes["b"].dispatched) == 1
    inner = nodes["b"].dispatched[0]
    assert inner.event_type is (FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK)
    assert inner.from_instance == "a"
    assert inner.routed_path == ["a", "b"]
    assert inner.payload == inner_payload
    # b did not forward; it only dispatched.
    assert all(
        s["event_type"] is not FederationEventType.SPACE_ROUTED
        for s in nodes["b"].fed.sent
    )


# ── Additional edge-case coverage ─────────────────────────────────────


def _make_envelope(
    *,
    route_id: str = "rid",
    path: list[str] | None = None,
    position: Any = 0,
    direction: str = "forward",
    inner_event_type: str = "space_invite_token_redeem",
    sealed: Any = None,
    from_instance: str = "a",
    to_instance: str = "b",
) -> FederationEvent:
    payload: dict[str, Any] = {
        "route_id": route_id,
        "path": list(path) if path is not None else ["a", "b"],
        "position": position,
        "direction": direction,
        "inner_event_type": inner_event_type,
        "sealed": sealed
        if sealed is not None
        else {"kem_suite": "x25519", "ciphertext": "x"},
    }
    return FederationEvent(
        msg_id="m-ec",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance=from_instance,
        to_instance=to_instance,
        timestamp="2026-05-22T00:00:00Z",
        payload=payload,
    )


async def test_send_routed_reply_raises_on_degenerate_reply_path():
    """A cached reply-state with a single-element path can never ship
    (no next hop) — :meth:`send_routed_reply` raises LookupError."""
    nodes = _build_chain(["a"])
    handler = nodes["a"].handler
    # Seed a degenerate reply-state with reply_path of length 1.
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    _opriv, opub = routed_crypto.generate_ephemeral_keypair()
    handler._reply_eph_state["rid-degen"] = (
        priv,
        pub,
        opub,
        ["a"],
        float("inf"),
    )
    with pytest.raises(LookupError, match="reply_path too short"):
        await handler.send_routed_reply(
            route_id="rid-degen",
            inner_event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
            inner_payload={},
        )


async def test_missing_position_is_dropped():
    """Envelope lacking ``position`` entirely is dropped."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = FederationEvent(
        msg_id="m-no-pos",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "r1",
            "path": ["a", "b"],
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"ciphertext": "x"},
        },
    )
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert nodes["b"].fed.sent == []


async def test_non_int_position_is_dropped():
    """Envelope whose ``position`` is a non-numeric string is dropped."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = _make_envelope(position="not-an-int", path=["a", "b"])
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert nodes["b"].fed.sent == []


async def test_position_out_of_range_is_dropped():
    """``position`` beyond the end of the path → drop."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = _make_envelope(position=99, path=["a", "b"])
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert nodes["b"].fed.sent == []


async def test_negative_position_is_dropped():
    """Negative ``position`` → drop."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = _make_envelope(position=-1, path=["a", "b"])
    await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []


async def test_from_instance_disagreeing_with_path_position_is_dropped(
    caplog,
):
    """The path[position] vs from_instance mismatch is an anti-spoof drop:
    the previous hop named in the path must be the authenticated sender, or
    the envelope is mis-routed / replayed off-path and must not be forwarded.
    """
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler
    nodes["b"].fed.sent.clear()
    nodes["b"].dispatched.clear()
    # path[0] = "z" but from_instance = "a" → mismatch → drop.
    ev = FederationEvent(
        msg_id="m-mismatch",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="a",
        to_instance="b",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-mismatch",
            "path": ["z", "b", "c"],
            "position": 0,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": {"kem_suite": "x25519", "ciphertext": "x"},
        },
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_b._on_routed(ev)
    # Dropped: not forwarded, not dispatched, and the mismatch warned.
    assert nodes["b"].fed.sent == []
    assert nodes["b"].dispatched == []
    assert any(
        "from_instance=" in rec.message and "dropping" in rec.message
        for rec in caplog.records
    )


async def test_forward_send_event_failure_is_logged(caplog):
    """When the federation send_event raises on the forward hop, the
    handler swallows it with a warning rather than propagating."""
    nodes = _build_chain(["a", "b", "c"])
    handler_b = nodes["b"].handler

    async def _boom(**kwargs):
        raise RuntimeError("transport down")

    nodes["b"].fed.send_event = _boom  # type: ignore[method-assign]
    ev = _make_envelope(
        route_id="rid-fwd-fail",
        path=["a", "b", "c", "d"],  # we are b at idx 1; forward to c.
        position=0,
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_b._on_routed(ev)
    assert any("forward to c failed" in r.message for r in caplog.records)


async def test_forward_unwrap_missing_target_eph_pk_drops(caplog):
    """Forward-leg unwrap where the sealed blob has no ``target_eph_pk``
    is dropped with a warning — we can't even look up the priv."""
    nodes = _build_chain(["a", "b"])
    handler_b = nodes["b"].handler
    ev = _make_envelope(
        route_id="rid-no-target-pub",
        path=["a", "b"],
        position=0,
        sealed={"kem_suite": "x25519", "ciphertext": "x"},  # no target_eph_pk
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_b._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert any("missing target_eph_pk" in r.message for r in caplog.records)


async def test_forward_unwrap_garbage_ciphertext_drops(caplog):
    """Forward-leg unwrap where AES-GCM decrypt fails on garbage
    ciphertext is dropped with a warning."""
    nodes = _build_chain(["a", "b"])
    pub = _mint_target_eph(nodes["b"])
    # Build a sealed blob that satisfies the wire shape but has a
    # tampered ciphertext that won't AES-decrypt.
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,  # any valid b64-style pub works for KEM derive
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = _make_envelope(
        route_id="rid-bad-ct",
        path=["a", "b"],
        position=0,
        sealed=sealed,
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await nodes["b"].handler._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert any("forward unseal failed" in r.message for r in caplog.records)


async def test_forward_unwrap_non_json_drops(caplog):
    """Forward-leg unseal that returns non-JSON triggers the JSON-parse
    error branch."""
    nodes = _build_chain(["a", "b"])
    pub = _mint_target_eph(nodes["b"])
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = _make_envelope(
        route_id="rid-non-json",
        path=["a", "b"],
        position=0,
        sealed=sealed,
    )
    with patch.object(routed_crypto, "unseal_inner_payload", return_value="not json"):
        with caplog.at_level(
            logging.WARNING, logger="socialhome.federation.routed_envelope"
        ):
            await nodes["b"].handler._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert any("JSON parse failed" in r.message for r in caplog.records)


async def test_forward_unwrap_non_dict_json_drops(caplog):
    """Forward-leg unseal that returns valid JSON but not a dict (e.g.
    a list) is dropped."""
    nodes = _build_chain(["a", "b"])
    pub = _mint_target_eph(nodes["b"])
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = _make_envelope(
        route_id="rid-non-dict",
        path=["a", "b"],
        position=0,
        sealed=sealed,
    )
    with patch.object(routed_crypto, "unseal_inner_payload", return_value="[]"):
        with caplog.at_level(
            logging.WARNING, logger="socialhome.federation.routed_envelope"
        ):
            await nodes["b"].handler._on_routed(ev)
    assert nodes["b"].dispatched == []
    assert any("decoded inner_payload is not a" in r.message for r in caplog.records)


async def test_reply_unwrap_without_origin_state_drops(caplog):
    """Reply-leg unwrap with no cached origin_eph_state for the
    route_id → drop + warn (expired or unsolicited reply)."""
    nodes = _build_chain(["a", "b"])
    handler_a = nodes["a"].handler
    # a is the origin (path[0]=a, path[-1]=b would be a normal forward;
    # for a reply leg the path is reversed: [b, a]). We are 'a' at
    # next_index=1.
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": "doesnt-matter",
        "target_eph_pk": "also-doesnt-matter",
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = FederationEvent(
        msg_id="m-no-origin-state",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-unsolicited",
            "path": ["b", "a"],
            "position": 0,
            "direction": "reply",
            "inner_event_type": "space_invite_token_redeem_ack",
            "sealed": sealed,
        },
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_a._on_routed(ev)
    assert nodes["a"].dispatched == []
    assert any("no cached origin_eph_priv" in r.message for r in caplog.records)


async def test_reply_unwrap_garbage_ciphertext_drops(caplog):
    """Reply-leg unseal that fails AES-GCM tag check → drop + warn."""
    nodes = _build_chain(["a", "b"])
    handler_a = nodes["a"].handler
    # Seed origin_eph_state so the cache lookup hits, but with a priv
    # that won't decrypt the bogus ciphertext.
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    handler_a._origin_eph_state["rid-reply-bad"] = (priv, pub, float("inf"))
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = FederationEvent(
        msg_id="m-reply-bad",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-reply-bad",
            "path": ["b", "a"],
            "position": 0,
            "direction": "reply",
            "inner_event_type": "space_invite_token_redeem_ack",
            "sealed": sealed,
        },
    )
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.routed_envelope"
    ):
        await handler_a._on_routed(ev)
    assert nodes["a"].dispatched == []
    assert any("reply unseal failed" in r.message for r in caplog.records)


async def test_reply_unwrap_non_json_drops(caplog):
    """Reply-leg unseal that returns non-JSON → JSON-parse drop."""
    nodes = _build_chain(["a", "b"])
    handler_a = nodes["a"].handler
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    handler_a._origin_eph_state["rid-reply-nojson"] = (priv, pub, float("inf"))
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = FederationEvent(
        msg_id="m-reply-nojson",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-reply-nojson",
            "path": ["b", "a"],
            "position": 0,
            "direction": "reply",
            "inner_event_type": "space_invite_token_redeem_ack",
            "sealed": sealed,
        },
    )
    with patch.object(routed_crypto, "unseal_reply_payload", return_value="not json"):
        with caplog.at_level(
            logging.WARNING, logger="socialhome.federation.routed_envelope"
        ):
            await handler_a._on_routed(ev)
    assert nodes["a"].dispatched == []
    assert any(
        "reply inner_payload JSON parse failed" in r.message for r in caplog.records
    )


async def test_reply_unwrap_non_dict_json_drops(caplog):
    """Reply-leg unseal that returns valid JSON but a list → drop."""
    nodes = _build_chain(["a", "b"])
    handler_a = nodes["a"].handler
    priv, pub = routed_crypto.generate_ephemeral_keypair()
    handler_a._origin_eph_state["rid-reply-list"] = (priv, pub, float("inf"))
    sealed = {
        "kem_suite": "x25519",
        "origin_eph_pk": pub,
        "target_eph_pk": pub,
        "nonce": "AAAAAAAAAAAAAAAA",
        "ciphertext": "AAAAAAAAAAAAAAAAAAAAAA",
    }
    ev = FederationEvent(
        msg_id="m-reply-list",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance="b",
        to_instance="a",
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": "rid-reply-list",
            "path": ["b", "a"],
            "position": 0,
            "direction": "reply",
            "inner_event_type": "space_invite_token_redeem_ack",
            "sealed": sealed,
        },
    )
    with patch.object(routed_crypto, "unseal_reply_payload", return_value="[]"):
        with caplog.at_level(
            logging.WARNING, logger="socialhome.federation.routed_envelope"
        ):
            await handler_a._on_routed(ev)
    assert nodes["a"].dispatched == []
    assert any("reply inner_payload is not a dict" in r.message for r in caplog.records)


# ── SPACE_ROUTE_STALE (route-stale nack) ─────────────────────────────

_RS_LOGGER = "socialhome.federation.routed_envelope"


def _seal_for_dead_target(route_id: str, dead_pub: str) -> dict:
    """Seal a forward-leg blob under ``dead_pub`` — a target ephemeral
    whose private half no node holds (the post-restart shape)."""
    o_priv, o_pub = routed_crypto.generate_ephemeral_keypair()
    return routed_crypto.seal_inner_payload(
        inner_payload_json='{"x":1}',
        origin_eph_priv_b64=o_priv,
        origin_eph_pub_b64=o_pub,
        target_eph_pub_b64=dead_pub,
        route_id=route_id,
        inner_event_type="space_invite_token_redeem",
    )


def _routed_at_target(
    *, path: list[str], route_id: str, sealed: dict
) -> FederationEvent:
    """A forward-leg envelope as it lands on ``path[-1]`` from ``path[-2]``."""
    return FederationEvent(
        msg_id="m-at-target",
        event_type=FederationEventType.SPACE_ROUTED,
        from_instance=path[-2],
        to_instance=path[-1],
        timestamp="2026-05-22T00:00:00Z",
        payload={
            "route_id": route_id,
            "path": list(path),
            "position": len(path) - 2,
            "direction": "forward",
            "inner_event_type": "space_invite_token_redeem",
            "sealed": sealed,
        },
    )


def _signed_nack(
    *,
    target: _Node,
    path: list[str],
    route_id: str,
    stale_pub: str,
    position: int | None = None,
    signer_seed: bytes | None = None,
    target_identity_pk: str | None = None,
) -> dict[str, Any]:
    """A ``SPACE_ROUTE_STALE`` payload as the target emits it (``position``
    defaults to ``len(path)-1``). ``signer_seed`` / ``target_identity_pk``
    let a test forge the signature or carry a foreign identity pk."""
    return {
        "route_id": route_id,
        "path": list(path),
        "position": len(path) - 1 if position is None else position,
        "target_identity_pk": target_identity_pk
        if target_identity_pk is not None
        else target.fed.own_identity_pk.hex(),
        "stale_eph_pk": stale_pub,
        "sig": routed_crypto.sign_route_stale(
            seed=signer_seed
            if signer_seed is not None
            else target.fed.own_identity_seed,
            route_id=route_id,
            stale_eph_pk_b64=stale_pub,
        ),
        "sig_suite": routed_crypto.ROUTE_STALE_SIG_SUITE_ED25519,
    }


def _nack_event(
    payload: dict[str, Any], *, from_instance: str, to_instance: str
) -> FederationEvent:
    return FederationEvent(
        msg_id="m-nack",
        event_type=FederationEventType.SPACE_ROUTE_STALE,
        from_instance=from_instance,
        to_instance=to_instance,
        timestamp="2026-05-22T00:00:00Z",
        payload=payload,
    )


def _stale_sends(node: _Node) -> list[dict[str, Any]]:
    return [
        s
        for s in node.fed.sent
        if s["event_type"] is FederationEventType.SPACE_ROUTE_STALE
    ]


async def test_missing_target_eph_priv_nacks_previous_hop(caplog):
    """The target has no private half for ``sealed.target_eph_pk`` (it
    restarted) and the previous hop speaks v_28 → exactly one signed
    ``SPACE_ROUTE_STALE`` goes back to ``path[-2]``; nothing is
    dispatched; the drop is logged at INFO (a handled condition), not
    WARNING."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    # c's target_eph_store stays empty → lookup returns None.
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    route_id = "r-stale-emit"
    ev = _routed_at_target(
        path=path, route_id=route_id, sealed=_seal_for_dead_target(route_id, dead_pub)
    )
    # Isolate the emitter: don't let b relay the nack onward here.
    c.fed.peers.clear()
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await c.handler._on_routed(ev)

    assert c.dispatched == []
    nacks = _stale_sends(c)
    assert len(nacks) == 1
    assert nacks[0]["to"] == b.instance_id
    payload = nacks[0]["payload"]
    assert set(payload) == {
        "route_id",
        "path",
        "position",
        "target_identity_pk",
        "stale_eph_pk",
        "sig",
        "sig_suite",
    }
    assert payload["route_id"] == route_id
    assert payload["path"] == path
    assert payload["position"] == len(path) - 1
    assert payload["stale_eph_pk"] == dead_pub
    assert payload["target_identity_pk"] == c.fed.own_identity_pk.hex()
    assert payload["sig_suite"] == routed_crypto.ROUTE_STALE_SIG_SUITE_ED25519
    assert routed_crypto.verify_route_stale(
        identity_pk=bytes.fromhex(payload["target_identity_pk"]),
        route_id=payload["route_id"],
        stale_eph_pk_b64=payload["stale_eph_pk"],
        sig_b64=payload["sig"],
        sig_suite=payload["sig_suite"],
    )
    # No content leaks into the nack — routing/validation fields only.
    assert "sealed" not in payload
    assert "inner_event_type" not in payload
    route_recs = [r for r in caplog.records if route_id[:8] in r.message]
    assert any(r.levelno == logging.INFO and "nacked" in r.message for r in route_recs)
    assert not any(r.levelno >= logging.WARNING for r in route_recs)


async def test_missing_target_eph_priv_pre_v28_prev_hop_drops_without_nack(
    caplog,
):
    """Previous hop is pre-v_28 → today's behaviour exactly: WARNING
    drop, no ``SPACE_ROUTE_STALE`` sent."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    c.fed.peer_versions[b.instance_id] = (
        FederationCapability.MIN_FOR_ROUTE_STALE_NACK - 1
    )
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    route_id = "r-stale-old"
    ev = _routed_at_target(
        path=path, route_id=route_id, sealed=_seal_for_dead_target(route_id, dead_pub)
    )
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await c.handler._on_routed(ev)
    assert c.dispatched == []
    assert _stale_sends(c) == []
    assert any(
        r.levelno == logging.WARNING
        and "no cached target_eph_priv" in r.message
        and "dropping" in r.message
        for r in caplog.records
    )
    assert any(
        r.levelno == logging.DEBUG and "pre-v_28" in r.message for r in caplog.records
    )


async def test_missing_target_eph_priv_nack_send_failure_is_logged(caplog):
    """A failing ``send_event`` while emitting the nack is fail-soft:
    WARNING + return, never a raise into the dispatcher."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    route_id = "r-stale-fail"
    ev = _routed_at_target(
        path=path, route_id=route_id, sealed=_seal_for_dead_target(route_id, dead_pub)
    )

    async def _boom(**_kw: Any) -> None:
        raise RuntimeError("transport down")

    with (
        patch.object(c.fed, "send_event", _boom),
        caplog.at_level(logging.DEBUG, logger=_RS_LOGGER),
    ):
        await c.handler._on_routed(ev)
    assert c.dispatched == []
    assert any(
        r.levelno == logging.WARNING and "nack" in r.message and "failed" in r.message
        for r in caplog.records
    )


async def test_route_stale_end_to_end_reaches_origin_stub(caplog):
    """Target emits → relay forwards → origin stub. One nack per hop,
    positions 2 then 1, no fan-out, nothing raised."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    route_id = "r-stale-e2e"
    ev = _routed_at_target(
        path=path, route_id=route_id, sealed=_seal_for_dead_target(route_id, dead_pub)
    )
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await c.handler._on_routed(ev)
        for _ in range(8):
            await asyncio.sleep(0)
    c_nacks = _stale_sends(c)
    b_nacks = _stale_sends(b)
    assert [n["to"] for n in c_nacks] == [b.instance_id]
    assert [n["to"] for n in b_nacks] == [a.instance_id]
    assert c_nacks[0]["payload"]["position"] == 2
    assert b_nacks[0]["payload"]["position"] == 1
    assert _stale_sends(a) == []
    # The origin never sent this route_id (the envelope was crafted at the
    # target), so the origin branch drops it as "not ours" — at DEBUG.
    assert any(
        "no pending routed send" in r.message and route_id[:8] in r.message
        for r in caplog.records
    )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_relay_forwards_toward_origin():
    """Relay at index 1 of a 3-hop path receives a valid nack with
    ``position=2`` from ``path[2]`` → forwards to ``path[0]`` with
    ``position=1``, every other field verbatim."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    nack = _signed_nack(target=c, path=path, route_id="r-relay", stale_pub=dead_pub)
    b.fed.peers.clear()  # don't deliver onward; inspect b's send only
    await b.handler._on_route_stale(
        _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
    )
    fwd = _stale_sends(b)
    assert len(fwd) == 1
    assert fwd[0]["to"] == a.instance_id
    assert fwd[0]["payload"] == {**nack, "position": 1}
    assert b.dispatched == []


async def test_route_stale_relay_drops_overlong_path(caplog):
    a, b, c = _build_identity_chain(3)
    long_path = (
        [a.instance_id, b.instance_id]
        + [f"n{i}" for i in range(_MAX_ROUTED_PATH_LEN - 2)]
        + [c.instance_id]
    )
    assert len(long_path) > _MAX_ROUTED_PATH_LEN
    nack = _signed_nack(target=c, path=long_path, route_id="r-long", stale_pub="p")
    # Make the position arithmetic otherwise valid for b at index 1.
    nack["position"] = 2
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await b.handler._on_route_stale(
            _nack_event(nack, from_instance=long_path[2], to_instance=b.instance_id)
        )
    assert _stale_sends(b) == []
    assert any(
        r.levelno == logging.WARNING and "path too long" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("bad_position", [0, 3, 99, -1, "two", None])
async def test_route_stale_relay_drops_out_of_range_position(bad_position):
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-pos", stale_pub="p")
    nack["position"] = bad_position
    await b.handler._on_route_stale(
        _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
    )
    assert _stale_sends(b) == []


@pytest.mark.parametrize(
    "missing",
    ["route_id", "path", "sig", "sig_suite", "target_identity_pk", "stale_eph_pk"],
)
async def test_route_stale_relay_drops_missing_field(missing):
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-miss", stale_pub="p")
    del nack[missing]
    await b.handler._on_route_stale(
        _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
    )
    assert _stale_sends(b) == []


async def test_route_stale_relay_drops_non_list_path():
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-path", stale_pub="p")
    nack["path"] = "not-a-list"
    await b.handler._on_route_stale(
        _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
    )
    assert _stale_sends(b) == []


async def test_route_stale_relay_drops_sender_not_at_position(caplog):
    """``path[position]`` must be the authenticated sender (mis-route/spoof)."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-spoof", stale_pub="p")
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await b.handler._on_route_stale(
            _nack_event(nack, from_instance="zzz", to_instance=b.instance_id)
        )
    assert _stale_sends(b) == []
    assert any(
        r.levelno == logging.WARNING and "mis-route/spoof" in r.message
        for r in caplog.records
    )


async def test_route_stale_relay_drops_wrong_next_hop(caplog):
    """``path[position-1]`` must be us — otherwise we're not on this leg."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, "someone-else", c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-wnh", stale_pub="p")
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await b.handler._on_route_stale(
            _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
        )
    assert _stale_sends(b) == []
    assert any(
        r.levelno == logging.WARNING and "wrong-next-hop" in r.message
        for r in caplog.records
    )


async def test_route_stale_relay_drops_identity_pk_not_deriving_to_target(caplog):
    """A ``target_identity_pk`` that doesn't derive to ``path[-1]`` can't be
    a genuine nack for this route — shed at the relay (WARNING)."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    other = generate_identity_keypair()
    nack = _signed_nack(
        target=c,
        path=path,
        route_id="r-derive",
        stale_pub="p",
        target_identity_pk=other.public_key.hex(),
    )
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await b.handler._on_route_stale(
            _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
        )
    assert _stale_sends(b) == []
    assert any(
        r.levelno == logging.WARNING and "target_identity_pk" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("bad_pk", ["zz", "abcd", "0" * 63, "0" * 66, ""])
async def test_route_stale_relay_drops_malformed_identity_pk(bad_pk):
    """Bad hex / wrong length → ``ValueError`` from decode/derive is caught → drop."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(
        target=c,
        path=path,
        route_id="r-badhex",
        stale_pub="p",
        target_identity_pk=bad_pk,
    )
    await b.handler._on_route_stale(
        _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
    )
    assert _stale_sends(b) == []


async def test_route_stale_relay_stops_at_pre_v28_next_hop(caplog):
    """Next hop toward the origin is pre-v_28 → the nack ends here
    (debug, no send) — behaviour degrades to today's for that peer."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    b.fed.peer_versions[a.instance_id] = (
        FederationCapability.MIN_FOR_ROUTE_STALE_NACK - 1
    )
    nack = _signed_nack(target=c, path=path, route_id="r-oldhop", stale_pub="p")
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await b.handler._on_route_stale(
            _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
        )
    assert _stale_sends(b) == []
    assert any(
        r.levelno == logging.DEBUG and "pre-v_28" in r.message for r in caplog.records
    )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_duplicate_is_forwarded_once():
    """Same ``route_id`` delivered twice → forwarded exactly once (dedup)."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-dup", stale_pub="p")
    b.fed.peers.clear()
    ev = _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
    await b.handler._on_route_stale(ev)
    await b.handler._on_route_stale(ev)
    assert len(_stale_sends(b)) == 1


async def test_route_stale_forged_structural_reject_does_not_poison_dedup():
    """A nack that fails the structural checks must NOT consume the
    ``route_id`` dedup slot — the genuine nack arriving afterwards still
    forwards."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-poison", stale_pub="p")
    b.fed.peers.clear()
    # Off-path sender → mis-route/spoof drop.
    await b.handler._on_route_stale(
        _nack_event(nack, from_instance="zzz", to_instance=b.instance_id)
    )
    assert _stale_sends(b) == []
    await b.handler._on_route_stale(
        _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
    )
    assert len(_stale_sends(b)) == 1


async def test_route_stale_relay_forward_failure_is_logged(caplog):
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-fwdfail", stale_pub="p")

    async def _boom(**_kw: Any) -> None:
        raise RuntimeError("transport down")

    with (
        patch.object(b.fed, "send_event", _boom),
        caplog.at_level(logging.DEBUG, logger=_RS_LOGGER),
    ):
        await b.handler._on_route_stale(
            _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
        )
    assert any(
        r.levelno == logging.WARNING
        and "SPACE_ROUTE_STALE" in r.message
        and "failed" in r.message
        for r in caplog.records
    )


async def test_route_stale_at_origin_invokes_origin_branch_and_does_not_forward(
    caplog,
):
    """``position - 1 == 0`` → we are the origin: the origin branch is
    invoked with the parsed fields and nothing is forwarded."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    nack = _signed_nack(
        target=c, path=path, route_id="r-origin", stale_pub=dead_pub, position=1
    )
    ev = _nack_event(nack, from_instance=b.instance_id, to_instance=a.instance_id)
    stub = AsyncMock()
    with patch.object(SpaceRoutedHandler, "_on_route_stale_at_origin", stub):
        await a.handler._on_route_stale(ev)
    assert a.fed.sent == []
    stub.assert_awaited_once()
    kw = stub.await_args.kwargs
    assert kw["event"] is ev
    assert kw["route_id"] == "r-origin"
    assert kw["path"] == path
    assert kw["target_identity_pk"] == c.fed.own_identity_pk.hex()
    assert kw["stale_eph_pk"] == dead_pub
    assert kw["sig"] == nack["sig"]
    assert kw["sig_suite"] == routed_crypto.ROUTE_STALE_SIG_SUITE_ED25519
    # The unpatched branch on a route_id we never sent: debug log, no
    # send, no raise.
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await a.handler._on_route_stale(
            _nack_event(
                {**nack, "route_id": "r-origin-2"},
                from_instance=b.instance_id,
                to_instance=a.instance_id,
            )
        )
    assert a.fed.sent == []
    assert any(
        "no pending routed send" in r.message and "r-origin-2"[:8] in r.message
        for r in caplog.records
    )


# ── SPACE_ROUTE_STALE at the origin (T3: verify → invalidate → retransmit) ──


class _FakeRouteService:
    """Recording stand-in for the three ``RouteDiscoveryService`` calls the
    origin branch makes: ``invalidate_if_eph``, ``discover_route`` and the
    pure ``cached_target_identity_pk`` read.

    ``live_eph`` models what the real cache currently points at: ``None``
    (default) means "whatever eph the nack names" so ``invalidate_if_eph``
    drops and records; set it to a different pub to model a route that was
    already rebuilt under a fresh key (``invalidate_if_eph`` → False)."""

    def __init__(self, *, discovery: tuple[list[str], str] | None = None) -> None:
        self.discovery = discovery
        self.invalidated: list[str] = []
        self.invalidate_eph_calls: list[tuple[str, str]] = []
        self.discover_calls: list[str] = []
        self.pins: dict[str, str] = {}
        self.live_eph: str | None = None

    async def invalidate_if_eph(
        self, target_instance_id: str, *, target_eph_pk: str
    ) -> bool:
        self.invalidate_eph_calls.append((target_instance_id, target_eph_pk))
        if self.live_eph is not None and self.live_eph != target_eph_pk:
            return False
        self.invalidated.append(target_instance_id)
        return True

    async def discover_route(
        self, target_instance_id: str
    ) -> tuple[list[str], str] | None:
        self.discover_calls.append(target_instance_id)
        return self.discovery

    def cached_target_identity_pk(self, target_instance_id: str) -> str | None:
        return self.pins.get(target_instance_id)


def _routed_sends(node: _Node) -> list[dict[str, Any]]:
    return [
        s for s in node.fed.sent if s["event_type"] is FederationEventType.SPACE_ROUTED
    ]


def _caching_flood(
    rs: RouteDiscoveryService,
    *,
    path: list[str],
    fresh_pub: str,
    identity_pk: str,
    delay_s: float = 0.0,
) -> AsyncMock:
    """A ``_flood_discover`` stand-in that behaves like the real one: it
    populates the route cache through ``_cache_route`` (so the next
    ``discover_route`` is a cache hit) and returns ``(path, fresh_pub)``.
    A mock that only returns the tuple leaves the cache empty and makes
    every subsequent nack look like a cache miss — hiding re-flood bugs."""

    async def _flood(*, target_instance_id: str, now: float, self_id: str) -> Any:
        if delay_s:
            await asyncio.sleep(delay_s)
        rs._cache_route(
            target_instance_id=target_instance_id,
            path=list(path),
            target_eph_pk=fresh_pub,
            target_identity_pk=identity_pk,
            anchored_at=now,
        )
        return list(path), fresh_pub

    return AsyncMock(side_effect=_flood)


async def _settle(rounds: int = 40) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def _origin_with_route_service(
    *, discovery: tuple[list[str], str] | None = None
) -> tuple[_Node, _Node, _Node, _FakeRouteService]:
    """A 3-node identity chain whose origin holds a recording route service
    pinning the target's identity pk (what ``send_with_mesh_fallback`` reads
    from ``cached_target_identity_pk`` right after discovery)."""
    a, b, c = _build_identity_chain(3)
    rs = _FakeRouteService(discovery=discovery)
    rs.pins[c.instance_id] = c.fed.own_identity_pk.hex()
    a.handler.attach_route_service(rs)  # type: ignore[arg-type]
    return a, b, c, rs


async def _send_under_dead_key(
    a: _Node,
    path: list[str],
    *,
    payload: dict[str, Any] | None = None,
    pin: str | None = None,
    inner_event_type: FederationEventType = FederationEventType.SPACE_POST_CREATED,
) -> tuple[str, str, dict[str, Any]]:
    """``send_routed`` along ``path`` under a target ephemeral nobody holds
    the private half for. Returns ``(route_id, dead_pub, payload)``."""
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    payload = payload if payload is not None else {"post_id": "p1", "n": 1}
    route_id = await a.handler.send_routed(
        path=path,
        target_eph_pk_b64=dead_pub,
        inner_event_type=inner_event_type,
        inner_payload=payload,
        target_identity_pk=pin if pin is not None else "",
    )
    return route_id, dead_pub, payload


def _origin_nack(
    *,
    target: _Node,
    path: list[str],
    route_id: str,
    stale_pub: str,
    **overrides: Any,
) -> dict[str, Any]:
    """A nack as it lands on the origin from ``path[1]`` (``position=1``)."""
    return (
        _signed_nack(
            target=target, path=path, route_id=route_id, stale_pub=stale_pub, position=1
        )
        | overrides
    )


async def _deliver_to_origin(a: _Node, nack: dict[str, Any]) -> None:
    await a.handler._on_route_stale(
        _nack_event(nack, from_instance=nack["path"][1], to_instance=a.instance_id)
    )


_RETRANSMIT_MSG = "invalidated, rediscovered, retransmitted"


async def test_route_stale_at_origin_verifies_invalidates_and_retransmits_once(
    caplog,
):
    """End to end: origin seals under a stale target key → target nacks →
    relay forwards → origin verifies against the pk it pinned at discovery,
    invalidates, rediscovers, and re-ships the SAME inner event once. The
    retransmit (sealed under the fresh key) unwraps at the target."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    fresh_pub = _mint_target_eph(c)
    rs.discovery = (list(path), fresh_pub)
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        route_id, _dead, payload = await _send_under_dead_key(
            a, path, pin=c.fed.own_identity_pk.hex()
        )
        await _settle()
    assert rs.invalidated == [c.instance_id]
    assert rs.discover_calls == [c.instance_id]
    routed = _routed_sends(a)
    assert len(routed) == 2, "expected the original + exactly one retransmit"
    new_route_id = routed[1]["payload"]["route_id"]
    assert new_route_id != route_id
    assert routed[1]["payload"]["inner_event_type"] == "space_post_created"
    assert routed[1]["payload"]["path"] == path
    assert routed[1]["payload"]["sealed"]["target_eph_pk"] == fresh_pub
    # The retransmit carried the identical inner event to the target.
    assert [e.event_type for e in c.dispatched] == [
        FederationEventType.SPACE_POST_CREATED
    ]
    assert c.dispatched[0].payload == payload
    assert c.dispatched[0].from_instance == a.instance_id
    assert c.dispatched[0].routed_route_id == new_route_id
    # Bookkeeping: the nacked entry is gone, the retry is marked as such.
    assert route_id not in a.handler._pending_routed
    retry = a.handler._pending_routed[new_route_id]
    assert retry.is_retry is True
    assert json.loads(retry.inner_payload_json) == payload
    assert retry.target_identity_pk == c.fed.own_identity_pk.hex()
    assert retry.target_eph_pk == fresh_pub
    assert rs.invalidate_eph_calls == [(c.instance_id, _dead)]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        _RETRANSMIT_MSG in r.message
        and route_id[:8] in r.message
        and new_route_id[:8] in r.message
        and c.instance_id in r.message
        and "space_post_created" in r.message
        for r in infos
    )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_unknown_route_id_is_dropped(caplog):
    """A nack for a route_id we never sent: no invalidate, no rediscover."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await _deliver_to_origin(
            a, _origin_nack(target=c, path=path, route_id="r-never", stale_pub=dead_pub)
        )
    assert rs.invalidated == []
    assert rs.discover_calls == []
    assert a.fed.sent == []
    assert any(
        r.levelno == logging.DEBUG and "no pending routed send" in r.message
        for r in caplog.records
    )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_expired_pending_is_dropped():
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    route_id, dead_pub, _ = await _send_under_dead_key(
        a, path, pin=c.fed.own_identity_pk.hex()
    )
    a.handler._pending_routed[route_id] = dataclasses.replace(
        a.handler._pending_routed[route_id], expires_at=time.monotonic() - 1.0
    )
    await _deliver_to_origin(
        a, _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    )
    assert rs.invalidated == []
    assert rs.discover_calls == []


async def test_route_stale_at_origin_target_mismatch_is_dropped(caplog):
    """Our route_id, but the nack names a different (self-consistent)
    target at ``path[-1]`` — dropped before any key material is touched."""
    nodes = _build_identity_chain(4)
    a, b, c, d = nodes
    rs = _FakeRouteService()
    rs.pins[c.instance_id] = c.fed.own_identity_pk.hex()
    a.handler.attach_route_service(rs)  # type: ignore[arg-type]
    a.fed.peers.clear()
    path = [a.instance_id, b.instance_id, c.instance_id]
    route_id, dead_pub, _ = await _send_under_dead_key(
        a, path, pin=c.fed.own_identity_pk.hex()
    )
    # Signed by d, carrying d's pk, path ending at d: passes every relay
    # structural check, but we sent this route_id to c.
    other_path = [a.instance_id, b.instance_id, d.instance_id]
    with (
        patch.object(routed_crypto, "verify_route_stale") as verify,
        caplog.at_level(logging.DEBUG, logger=_RS_LOGGER),
    ):
        await _deliver_to_origin(
            a,
            _origin_nack(
                target=d, path=other_path, route_id=route_id, stale_pub=dead_pub
            ),
        )
    verify.assert_not_called()
    assert rs.invalidated == []
    assert route_id in a.handler._pending_routed
    assert any("different target" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_pinned_pk_mismatch_drops_before_signature(
    caplog,
):
    """The pk we pinned at discovery is the authority. A nack whose
    ``target_identity_pk`` differs from the pin is dropped BEFORE the
    signature is even checked (simulated by pinning a foreign key)."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    foreign = generate_identity_keypair()
    route_id, dead_pub, _ = await _send_under_dead_key(
        a, path, pin=foreign.public_key.hex()
    )
    genuine = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    with (
        patch.object(routed_crypto, "verify_route_stale") as verify,
        caplog.at_level(logging.DEBUG, logger=_RS_LOGGER),
    ):
        await _deliver_to_origin(a, genuine)
    verify.assert_not_called()
    assert rs.invalidated == []
    assert rs.discover_calls == []
    assert route_id in a.handler._pending_routed
    assert any("pinned" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_empty_pin_falls_back_to_derive_binding(caplog):
    """A legacy caller that passed no pin: the nack is still bound to the
    target by ``derive_instance_id`` and the signature — an empty pin is
    never acceptance on its own (a forged signature is still rejected)."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    route_id, dead_pub, _ = await _send_under_dead_key(a, path, pin=None)
    attacker = generate_identity_keypair()
    forged = _origin_nack(
        target=c,
        path=path,
        route_id=route_id,
        stale_pub=dead_pub,
        sig=routed_crypto.sign_route_stale(
            seed=attacker.private_key, route_id=route_id, stale_eph_pk_b64=dead_pub
        ),
    )
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await _deliver_to_origin(a, forged)
    assert rs.invalidated == []
    assert route_id in a.handler._pending_routed
    # Genuine nack (delivered directly: the hop-level dedup already burned
    # this route_id) is accepted, with the missing pin noted at DEBUG.
    genuine = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await a.handler._on_route_stale_at_origin(
            event=_nack_event(
                genuine, from_instance=b.instance_id, to_instance=a.instance_id
            ),
            route_id=route_id,
            path=path,
            target_identity_pk=genuine["target_identity_pk"],
            stale_eph_pk=dead_pub,
            sig=genuine["sig"],
            sig_suite=genuine["sig_suite"],
        )
    assert rs.invalidated == [c.instance_id]
    assert any(
        r.levelno == logging.DEBUG and "pin" in r.message and "unavailable" in r.message
        for r in caplog.records
    )


async def test_route_stale_at_origin_forged_signature_is_dropped(caplog):
    """Attacker key, target's pk carried verbatim (passes relay checks and
    the pin check) → signature fails → no invalidate, pending kept."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    route_id, dead_pub, _ = await _send_under_dead_key(
        a, path, pin=c.fed.own_identity_pk.hex()
    )
    attacker = generate_identity_keypair()
    forged = _origin_nack(
        target=c,
        path=path,
        route_id=route_id,
        stale_pub=dead_pub,
        sig=routed_crypto.sign_route_stale(
            seed=attacker.private_key, route_id=route_id, stale_eph_pk_b64=dead_pub
        ),
    )
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await _deliver_to_origin(a, forged)
    assert rs.invalidated == []
    assert rs.discover_calls == []
    assert route_id in a.handler._pending_routed
    assert any(
        r.levelno == logging.INFO and "signature" in r.message for r in caplog.records
    )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_unknown_sig_suite_is_dropped(caplog):
    """``UnsupportedRouteStaleSuite`` is raised inside and swallowed — no
    default-algorithm fallback, no invalidate."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    route_id, dead_pub, _ = await _send_under_dead_key(
        a, path, pin=c.fed.own_identity_pk.hex()
    )
    nack = _origin_nack(
        target=c,
        path=path,
        route_id=route_id,
        stale_pub=dead_pub,
        sig_suite="ed25519+mldsa65",
    )
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await _deliver_to_origin(a, nack)
    assert rs.invalidated == []
    assert route_id in a.handler._pending_routed
    assert any("sig_suite" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_malformed_identity_hex_is_dropped():
    """Defence in depth below the relay's hex check: a bad pk at the origin
    branch itself is a drop, not a raise."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    route_id, dead_pub, _ = await _send_under_dead_key(a, path, pin=None)
    nack = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    await a.handler._on_route_stale_at_origin(
        event=_nack_event(nack, from_instance=b.instance_id, to_instance=a.instance_id),
        route_id=route_id,
        path=path,
        target_identity_pk="zz",
        stale_eph_pk=dead_pub,
        sig=nack["sig"],
        sig_suite=nack["sig_suite"],
    )
    assert rs.invalidated == []
    assert route_id in a.handler._pending_routed


async def test_route_stale_at_origin_replay_is_a_noop():
    """The pending entry is popped on the first valid nack — a replayed copy
    (bypassing the hop-level dedup) finds nothing: one invalidate, one
    rediscover, one retransmit."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    rs.discovery = (list(path), _mint_target_eph(c))
    route_id, dead_pub, _ = await _send_under_dead_key(
        a, path, pin=c.fed.own_identity_pk.hex()
    )
    nack = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    kwargs = dict(
        event=_nack_event(nack, from_instance=b.instance_id, to_instance=a.instance_id),
        route_id=route_id,
        path=path,
        target_identity_pk=nack["target_identity_pk"],
        stale_eph_pk=dead_pub,
        sig=nack["sig"],
        sig_suite=nack["sig_suite"],
    )
    await a.handler._on_route_stale_at_origin(**kwargs)
    await a.handler._on_route_stale_at_origin(**kwargs)
    assert rs.invalidated == [c.instance_id]
    assert rs.discover_calls == [c.instance_id]
    assert len(_routed_sends(a)) == 2  # original + one retransmit
    assert route_id not in a.handler._pending_routed


async def test_route_stale_at_origin_nacked_retry_gives_up(caplog):
    """Rediscovery hands back another dead key → the retransmit is nacked
    too → invalidated again but no third send (≤ 2 attempts per original)."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    _dead2_priv, dead2_pub = routed_crypto.generate_ephemeral_keypair()
    rs.discovery = (list(path), dead2_pub)
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        route_id, _dead, _ = await _send_under_dead_key(
            a, path, pin=c.fed.own_identity_pk.hex()
        )
        await _settle()
    routed = _routed_sends(a)
    assert len(routed) == 2
    new_route_id = routed[1]["payload"]["route_id"]
    assert rs.invalidated == [c.instance_id, c.instance_id]
    assert rs.discover_calls == [c.instance_id]
    assert new_route_id not in a.handler._pending_routed
    assert c.dispatched == []
    assert any(
        r.levelno == logging.INFO
        and "giving up" in r.message
        and new_route_id[:8] in r.message
        for r in caplog.records
    )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_oversized_inner_invalidates_without_retransmit(
    caplog,
):
    """An inner over ``_MAX_RETAINED_INNER_BYTES`` is retained with
    ``inner_payload_json=None`` — the nack still invalidates the route, but
    nothing is retransmitted (the durable outbox re-sends media chunks)."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    rs.discovery = (list(path), _mint_target_eph(c))
    big = {"chunk": "x" * (_MAX_RETAINED_INNER_BYTES + 1)}
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        route_id, _dead, _ = await _send_under_dead_key(
            a, path, payload=big, pin=c.fed.own_identity_pk.hex()
        )
        pending = a.handler._pending_routed[route_id]
        assert pending.inner_payload_json is None
        assert pending.target_instance_id == c.instance_id
        await _settle()
    assert rs.invalidated == [c.instance_id]
    assert rs.discover_calls == []
    assert len(_routed_sends(a)) == 1
    assert route_id not in a.handler._pending_routed
    assert any(
        r.levelno == logging.INFO and "oversized" in r.message for r in caplog.records
    )


async def test_route_stale_at_origin_small_inner_is_retained_intact():
    a, b, c, _rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    payload = {"k": "v", "n": [1, 2, 3]}
    route_id, _dead, _ = await _send_under_dead_key(
        a, path, payload=payload, pin=c.fed.own_identity_pk.hex()
    )
    pending = a.handler._pending_routed[route_id]
    # The retained form IS the measured wire bytes — compact, sorted JSON.
    assert pending.inner_payload_json == json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )
    assert json.loads(pending.inner_payload_json) == payload
    assert pending.target_eph_pk == _dead
    assert pending.inner_event_type is FederationEventType.SPACE_POST_CREATED
    assert pending.is_retry is False
    assert pending.expires_at > time.monotonic()


async def test_route_stale_at_origin_nack_storm_collapses_into_one_flood():
    """Amplification bound: 20 valid nacks for 20 route_ids to one target,
    concurrently → a REAL ``discover_route`` single-flights them into one
    ``_flood_discover``; retransmits are one per original (20), never more."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    rs = RouteDiscoveryService(
        federation_service=a.fed,  # type: ignore[arg-type]
        federation_repo=a.repo,  # type: ignore[arg-type]
    )
    a.handler.attach_route_service(rs)
    fresh_pub = _mint_target_eph(c)
    flood = _caching_flood(
        rs,
        path=path,
        fresh_pub=fresh_pub,
        identity_pk=c.fed.own_identity_pk.hex(),
        delay_s=0.01,
    )
    sent: list[tuple[str, str]] = []
    for i in range(20):
        route_id, dead_pub, _ = await _send_under_dead_key(
            a,
            path,
            payload={"post_id": f"p{i}"},
            pin=c.fed.own_identity_pk.hex(),
        )
        sent.append((route_id, dead_pub))
    assert len(_routed_sends(a)) == 20

    async def _nack(route_id: str, dead_pub: str) -> None:
        nack = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
        await a.handler._on_route_stale_at_origin(
            event=_nack_event(
                nack, from_instance=b.instance_id, to_instance=a.instance_id
            ),
            route_id=route_id,
            path=path,
            target_identity_pk=nack["target_identity_pk"],
            stale_eph_pk=dead_pub,
            sig=nack["sig"],
            sig_suite=nack["sig_suite"],
        )

    with patch.object(RouteDiscoveryService, "_flood_discover", flood):
        await asyncio.gather(*(_nack(r, d) for r, d in sent))
    assert flood.await_count == 1, "nack storm was not collapsed into one flood"
    routed = _routed_sends(a)
    assert len(routed) == 40  # 20 originals + exactly one retransmit each
    retransmitted = sorted(
        json.loads(
            routed_crypto.unseal_inner_payload(
                sealed=s["payload"]["sealed"],
                target_eph_priv_b64=c.target_eph_store[fresh_pub],
                route_id=s["payload"]["route_id"],
                inner_event_type=s["payload"]["inner_event_type"],
            )
        )["post_id"]
        for s in routed[20:]
    )
    assert retransmitted == sorted(f"p{i}" for i in range(20))
    assert all(
        a.handler._pending_routed[s["payload"]["route_id"]].is_retry
        for s in routed[20:]
    )


async def test_route_stale_at_origin_no_route_on_rediscovery_gives_up(caplog):
    a, b, c, rs = _origin_with_route_service(discovery=None)
    path = [a.instance_id, b.instance_id, c.instance_id]
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        route_id, _dead, _ = await _send_under_dead_key(
            a, path, pin=c.fed.own_identity_pk.hex()
        )
        await _settle()
    assert rs.invalidated == [c.instance_id]
    assert rs.discover_calls == [c.instance_id]
    assert len(_routed_sends(a)) == 1
    assert route_id not in a.handler._pending_routed
    assert any(
        r.levelno == logging.INFO
        and "no route" in r.message
        and route_id[:8] in r.message
        for r in caplog.records
    )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_without_route_service_logs_and_returns(caplog):
    """Verified nack but ``attach_route_service`` never ran: the pending
    entry is consumed, nothing is invalidated or re-sent, nothing raises."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        route_id, _dead, _ = await _send_under_dead_key(
            a, path, pin=c.fed.own_identity_pk.hex()
        )
        await _settle()
    assert len(_routed_sends(a)) == 1
    assert route_id not in a.handler._pending_routed
    assert any(
        r.levelno == logging.INFO and "no route service" in r.message
        for r in caplog.records
    )
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_retransmit_failure_is_a_warning(caplog):
    """A transport failure on the retransmit is the ONE warning-worthy
    outcome — logged with the traceback, never raised into the dispatcher."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    rs.discovery = (list(path), _mint_target_eph(c))
    route_id, dead_pub, _ = await _send_under_dead_key(
        a, path, pin=c.fed.own_identity_pk.hex()
    )

    async def _boom(**_kw: Any) -> None:
        raise RuntimeError("transport down")

    nack = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    with (
        patch.object(a.fed, "send_event", _boom),
        caplog.at_level(logging.DEBUG, logger=_RS_LOGGER),
    ):
        await _deliver_to_origin(a, nack)
    assert rs.invalidated == [c.instance_id]
    assert any(
        r.levelno == logging.WARNING
        and "retransmit" in r.message
        and r.exc_info is not None
        for r in caplog.records
    )


def test_pending_routed_is_pruned_by_expiry_and_capped():
    a, *_ = _build_identity_chain(1)
    h = a.handler
    now = time.monotonic()

    def _entry(expires_at: float):
        return dataclasses.replace(_pending_template(), expires_at=expires_at)

    h._pending_routed["old"] = _entry(now - 1.0)
    for i in range(_MAX_PENDING_ROUTED + 5):
        h._pending_routed[f"r{i}"] = _entry(now + 10.0 + i)
    h._prune_expired(now)
    assert "old" not in h._pending_routed
    assert len(h._pending_routed) == _MAX_PENDING_ROUTED
    # Oldest-by-expiry evicted first: the 5 lowest indices are gone.
    assert "r0" not in h._pending_routed and "r4" not in h._pending_routed
    assert f"r{_MAX_PENDING_ROUTED + 4}" in h._pending_routed


def _pending_template() -> _PendingRouted:
    return _PendingRouted(
        target_instance_id="t",
        target_identity_pk="",
        target_eph_pk="",
        inner_event_type=FederationEventType.SPACE_POST_CREATED,
        inner_payload_json="{}",
        is_retry=False,
        expires_at=0.0,
    )


async def test_route_stale_at_origin_empty_pin_rejects_pk_deriving_elsewhere(caplog):
    """Empty pin + a pk that derives to a different instance than the one we
    sent to (direct call: the relay step would normally shed this via
    ``path[-1]``) — the fallback derive binding is against OUR record."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    route_id, dead_pub, _ = await _send_under_dead_key(a, path, pin=None)
    other = generate_identity_keypair()
    nack = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await a.handler._on_route_stale_at_origin(
            event=_nack_event(
                nack, from_instance=b.instance_id, to_instance=a.instance_id
            ),
            route_id=route_id,
            path=path,
            target_identity_pk=other.public_key.hex(),
            stale_eph_pk=dead_pub,
            sig=nack["sig"],
            sig_suite=nack["sig_suite"],
        )
    assert rs.invalidated == []
    assert route_id in a.handler._pending_routed
    assert any("not our target" in r.message for r in caplog.records)


async def test_route_stale_at_origin_matching_but_malformed_pin_is_dropped(caplog):
    """Defensive: a pin that matches the carried pk but is not valid hex
    cannot be verified — ``ValueError`` from decode is a drop, not a raise."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    route_id, dead_pub, _ = await _send_under_dead_key(a, path, pin="zz")
    nack = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await a.handler._on_route_stale_at_origin(
            event=_nack_event(
                nack, from_instance=b.instance_id, to_instance=a.instance_id
            ),
            route_id=route_id,
            path=path,
            target_identity_pk="zz",
            stale_eph_pk=dead_pub,
            sig=nack["sig"],
            sig_suite=nack["sig_suite"],
        )
    assert rs.invalidated == []
    assert route_id in a.handler._pending_routed
    assert any("malformed target_identity_pk" in r.message for r in caplog.records)


# ── Adversarial-review regressions: eph binding, growth-path prune, ─────
# ── retained wire bytes, delivery-result logging ────────────────────────


async def test_route_stale_at_origin_eph_not_sealed_under_is_dropped(caplog):
    """Signing-oracle shape (I-1): an on-path relay swaps
    ``sealed.target_eph_pk`` for garbage P', the target (holding no priv for
    P') genuinely signs a nack over ``(route_id, P')``. The origin must
    refuse it BEFORE the signature check — it never sealed under P' for this
    route_id — so a live route is not torn down, nothing is rediscovered or
    retransmitted, and the pending record survives for the real nack."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    rs.discovery = (list(path), _mint_target_eph(c))
    a.fed.peers.clear()  # hand-craft the nack; nothing is delivered
    live_pub = _mint_target_eph(c)
    route_id = await a.handler.send_routed(
        path=path,
        target_eph_pk_b64=live_pub,
        inner_event_type=FederationEventType.SPACE_POST_CREATED,
        inner_payload={"post_id": "p1"},
        target_identity_pk=c.fed.own_identity_pk.hex(),
    )
    _p, unrelated_pub = routed_crypto.generate_ephemeral_keypair()
    nack = _origin_nack(target=c, path=path, route_id=route_id, stale_pub=unrelated_pub)
    # Genuinely signed by the target over the substituted key…
    assert routed_crypto.verify_route_stale(
        identity_pk=c.fed.own_identity_pk,
        route_id=route_id,
        stale_eph_pk_b64=unrelated_pub,
        sig_b64=nack["sig"],
        sig_suite=nack["sig_suite"],
    )
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        await _deliver_to_origin(a, nack)
    # …and still worthless to the relay.
    assert rs.invalidated == []
    assert rs.invalidate_eph_calls == []
    assert rs.discover_calls == []
    assert len(_routed_sends(a)) == 1
    assert route_id in a.handler._pending_routed
    assert any(
        r.levelno == logging.DEBUG
        and route_id[:8] in r.message
        and "never sealed under" in r.message
        for r in caplog.records
    )
    assert not any(r.levelno >= logging.INFO for r in caplog.records)


async def test_route_stale_at_origin_late_nack_after_rebuild_retransmits_without_invalidating(
    caplog,
):
    """The route was already rebuilt under a fresh key (a sibling nack got
    there first). A late genuine nack for the OLD key must not evict the
    fresh route — ``invalidate_if_eph`` says no — but the lost envelope is
    still retransmitted over the (cache-hit) fresh route."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    fresh_pub = _mint_target_eph(c)
    rs.discovery = (list(path), fresh_pub)
    rs.live_eph = fresh_pub  # cache already points at the fresh key
    with caplog.at_level(logging.DEBUG, logger=_RS_LOGGER):
        route_id, dead_pub, payload = await _send_under_dead_key(
            a, path, pin=c.fed.own_identity_pk.hex()
        )
        await _settle()
    assert rs.invalidate_eph_calls == [(c.instance_id, dead_pub)]
    assert rs.invalidated == []
    assert rs.discover_calls == [c.instance_id]
    routed = _routed_sends(a)
    assert len(routed) == 2
    assert routed[1]["payload"]["sealed"]["target_eph_pk"] == fresh_pub
    assert [e.payload for e in c.dispatched] == [payload]
    assert route_id not in a.handler._pending_routed
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "already rebuilt" in r.message and route_id[:8] in r.message for r in infos
    )
    assert not any(_RETRANSMIT_MSG in r.message for r in infos)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_route_stale_at_origin_sequential_nacks_flood_once():
    """Five envelopes sealed under P_old; the target restarts (its eph
    state wiped); the five genuine nacks arrive one AFTER another. Nack 1
    floods and caches P_new; nacks 2..5 must find the fresh route still
    cached (their stale key is not what the cache points at) → exactly one
    flood, five retransmits, every retransmit sealed under P_new."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    rs = RouteDiscoveryService(
        federation_service=a.fed,  # type: ignore[arg-type]
        federation_repo=a.repo,  # type: ignore[arg-type]
    )
    a.handler.attach_route_service(rs)
    _old_priv, old_pub = routed_crypto.generate_ephemeral_keypair()
    sent: list[str] = []
    for i in range(5):
        sent.append(
            await a.handler.send_routed(
                path=path,
                target_eph_pk_b64=old_pub,
                inner_event_type=FederationEventType.SPACE_POST_CREATED,
                inner_payload={"post_id": f"p{i}"},
                target_identity_pk=c.fed.own_identity_pk.hex(),
            )
        )
    # Target restarts: fresh eph, old private half gone.
    c.target_eph_store.clear()
    fresh_pub = _mint_target_eph(c)
    flood = _caching_flood(
        rs, path=path, fresh_pub=fresh_pub, identity_pk=c.fed.own_identity_pk.hex()
    )
    with patch.object(RouteDiscoveryService, "_flood_discover", flood):
        for route_id in sent:
            await _deliver_to_origin(
                a,
                _origin_nack(target=c, path=path, route_id=route_id, stale_pub=old_pub),
            )
            assert rs.cached_target_identity_pk(c.instance_id) is not None, (
                "a later nack evicted the freshly rebuilt route"
            )
    assert flood.await_count == 1, f"{flood.await_count} floods for 5 sequential nacks"
    routed = _routed_sends(a)
    assert len(routed) == 10  # 5 originals + exactly one retransmit each
    assert all(s["payload"]["sealed"]["target_eph_pk"] == fresh_pub for s in routed[5:])
    retransmitted = sorted(
        json.loads(
            routed_crypto.unseal_inner_payload(
                sealed=s["payload"]["sealed"],
                target_eph_priv_b64=c.target_eph_store[fresh_pub],
                route_id=s["payload"]["route_id"],
                inner_event_type=s["payload"]["inner_event_type"],
            )
        )["post_id"]
        for s in routed[5:]
    )
    assert retransmitted == [f"p{i}" for i in range(5)]
    assert all(route_id not in a.handler._pending_routed for route_id in sent)


async def test_send_routed_prunes_pending_on_the_growth_path():
    """M-1: a household sending many routed events to a down target with no
    inbound routed traffic must not grow ``_pending_routed`` unbounded —
    ``send_routed`` itself reaps expired entries and enforces the cap. The
    prune runs BEFORE the new record is inserted (so the send in flight is
    never the entry evicted), hence at rest the dict holds at most the cap
    plus that one live send."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    a.fed.peers.clear()
    h = a.handler
    now = time.monotonic()
    h._pending_routed["expired"] = dataclasses.replace(
        _pending_template(), expires_at=now - 1.0
    )
    for i in range(_MAX_PENDING_ROUTED + 5):
        h._pending_routed[f"r{i}"] = dataclasses.replace(
            _pending_template(), expires_at=now + 10.0 + i
        )
    new_ids: list[str] = []
    for _ in range(3):
        new_ids.append((await _send_under_dead_key(a, path))[0])
        assert len(h._pending_routed) <= _MAX_PENDING_ROUTED + 1
    assert "expired" not in h._pending_routed
    # The live sends (latest expiry) survive; the oldest seeded ones went.
    assert all(rid in h._pending_routed for rid in new_ids)
    assert "r0" not in h._pending_routed


async def test_send_routed_retains_the_measured_wire_bytes_not_the_callers_dict():
    """M-2: mutating a nested structure in the caller's dict after
    ``send_routed`` must not change what a nack retransmits — the retained
    form is the exact compact JSON that was measured and sealed."""
    a, b, c, rs = _origin_with_route_service()
    path = [a.instance_id, b.instance_id, c.instance_id]
    fresh_pub = _mint_target_eph(c)
    rs.discovery = (list(path), fresh_pub)
    a.fed.peers.clear()
    payload: dict[str, Any] = {"post_id": "p1", "tags": ["a"], "meta": {"n": 1}}
    original = json.loads(json.dumps(payload))
    route_id, dead_pub, _ = await _send_under_dead_key(
        a, path, payload=payload, pin=c.fed.own_identity_pk.hex()
    )
    payload["tags"].append("x" * _MAX_RETAINED_INNER_BYTES)  # would exceed the cap
    payload["meta"]["n"] = 2
    await _deliver_to_origin(
        a, _origin_nack(target=c, path=path, route_id=route_id, stale_pub=dead_pub)
    )
    routed = _routed_sends(a)
    assert len(routed) == 2
    carried = json.loads(
        routed_crypto.unseal_inner_payload(
            sealed=routed[1]["payload"]["sealed"],
            target_eph_priv_b64=c.target_eph_store[fresh_pub],
            route_id=routed[1]["payload"]["route_id"],
            inner_event_type=routed[1]["payload"]["inner_event_type"],
        )
    )
    assert carried == original


async def test_missing_target_eph_priv_nack_not_delivered_is_a_warning(caplog):
    """N-1: ``send_event`` reports failure via ``DeliveryResult(ok=False)``
    rather than raising. A nack that never left is the pre-v_28 silent
    drop — WARNING, and no "nacked to" success INFO."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    _dead_priv, dead_pub = routed_crypto.generate_ephemeral_keypair()
    route_id = "r-stale-notok"
    ev = _routed_at_target(
        path=path, route_id=route_id, sealed=_seal_for_dead_target(route_id, dead_pub)
    )
    failed = AsyncMock(
        return_value=DeliveryResult(
            instance_id=b.instance_id, ok=False, error="unreachable"
        )
    )
    with (
        patch.object(c.fed, "send_event", failed),
        caplog.at_level(logging.DEBUG, logger=_RS_LOGGER),
    ):
        await c.handler._on_routed(ev)
    assert failed.await_count == 1
    route_recs = [r for r in caplog.records if route_id[:8] in r.message]
    assert any(
        r.levelno == logging.WARNING
        and "SPACE_ROUTE_STALE" in r.message
        and "not delivered" in r.message
        and b.instance_id in r.message
        for r in route_recs
    )
    assert not any(
        r.levelno == logging.INFO and "nacked to" in r.message for r in route_recs
    )


async def test_route_stale_relay_forward_not_delivered_is_a_warning(caplog):
    """N-1 (relay leg): a forward the transport rejected with ``ok=False``
    is logged at WARNING, not swallowed."""
    a, b, c = _build_identity_chain(3)
    path = [a.instance_id, b.instance_id, c.instance_id]
    nack = _signed_nack(target=c, path=path, route_id="r-fwd-notok", stale_pub="p")
    failed = AsyncMock(
        return_value=DeliveryResult(
            instance_id=a.instance_id, ok=False, error="unreachable"
        )
    )
    with (
        patch.object(b.fed, "send_event", failed),
        caplog.at_level(logging.DEBUG, logger=_RS_LOGGER),
    ):
        await b.handler._on_route_stale(
            _nack_event(nack, from_instance=c.instance_id, to_instance=b.instance_id)
        )
    assert failed.await_count == 1
    assert any(
        r.levelno == logging.WARNING
        and "SPACE_ROUTE_STALE" in r.message
        and "not delivered" in r.message
        and a.instance_id in r.message
        for r in caplog.records
    )

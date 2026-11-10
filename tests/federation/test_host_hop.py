"""The space host as the one ``SPACE_ROUTED`` hop toward a carded member (v_89).

A member with no connection to the GFS another member reaches the space
through sends ``[member, host, member]``, sealed to the target's key-wrap
key from its card. The host relays without reading; the target unwraps
with its key-wrap key. A ``space_session`` seat may send / receive a routed
envelope in exactly that shape — nothing else.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import (
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
    InstanceSource,
)
from socialhome.federation.host_hop import HOST_HOP_UNAVAILABLE, HostHop
from socialhome.federation.member_card import build_card
from tests.federation.test_routed_envelope import (
    _build_chain,
    _FakeInstance,
    _Node,
)

SPACE = "sp-hop"


def _identity_chain() -> tuple[list[str], dict]:
    idents = [generate_identity_keypair() for _ in range(3)]
    ids = [derive_instance_id(k.public_key) for k in idents]
    return ids, dict(zip(ids, idents, strict=True))


def _carry_space_id(node: _Node) -> None:
    """The linked test transport, carrying the routing ``space_id`` the way
    the real envelope does (the host hop's checks read it)."""
    fed = node.fed

    async def send_event(
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> Any:
        fed.sent.append(
            {"to": to_instance_id, "event_type": event_type, "space_id": space_id}
        )
        dest = fed.peers.get(to_instance_id)
        if dest is None or event_type is not FederationEventType.SPACE_ROUTED:
            return SimpleNamespace(ok=True, instance_id=to_instance_id)
        ev = FederationEvent(
            msg_id=f"m-{len(fed.sent)}",
            event_type=event_type,
            from_instance=fed.own_instance_id,
            to_instance=to_instance_id,
            timestamp="2026-11-09T00:00:00Z",
            payload=payload,
            space_id=space_id,
        )
        asyncio.create_task(dest._on_routed(ev))
        return SimpleNamespace(ok=True, instance_id=to_instance_id)

    fed.send_event = send_event  # type: ignore[method-assign]


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _wire(*, cards_at: dict[str, set[str]], host: str | None = None):
    """Three nodes ``origin → host → target`` (instance ids derived from
    their identities). The origin and the target are ``space_session``
    seats of the host and vice versa; ``cards_at[node]`` lists whose cards
    that node holds for :data:`SPACE`."""
    ids, idents = _identity_chain()
    origin, mid, target = ids
    nodes = _build_chain(ids, identities=idents)
    keywraps = {i: generate_x25519_keypair() for i in ids}
    cards = {
        i: build_card(
            instance_id=i,
            identity_seed=idents[i].private_key,
            identity_pk=idents[i].public_key,
            keywrap_pk=keywraps[i].public_key,
            keywrap_sig=b64url_encode(
                sign_ed25519(idents[i].private_key, keywraps[i].public_key)
            ),
            proto_version=89,
            rev=1,
            mailbox=None,
        )
        for i in ids
    }
    space_host = host or mid
    role = {origin: "origin", mid: "host", target: "target"}
    for node_id, node in nodes.items():
        _carry_space_id(node)
        holds = cards_at.get(role[node_id], set())

        async def card_of(space_id, iid, holds=holds):
            if space_id != SPACE or iid not in role:
                return None
            return cards[iid] if role[iid] in holds else None

        async def host_of(space_id):
            return space_host if space_id == SPACE else None

        hop = HostHop(
            own_instance_id=node_id,
            card_of=card_of,
            host_of=host_of,
            keywrap_private_key=keywraps[node_id].private_key,
            keywrap_public_key=keywraps[node_id].public_key,
        )
        node.handler.attach_host_hop(hop)
    # The ends are each other's seats through the host only: the origin
    # and the target are space_session seats OF the host and vice versa.
    for a, b in ((mid, origin), (mid, target), (origin, mid), (target, mid)):
        nodes[a].repo._instances[b] = _FakeInstance(
            id=b, source=InstanceSource.SPACE_SESSION
        )
    return nodes, (origin, mid, target)


def _hop(node: _Node) -> HostHop:
    hop = node.handler._host_hop
    assert hop is not None
    return hop


async def test_a_member_reaches_another_through_the_host_sealed_end_to_end():
    nodes, (origin, host, target) = _wire(
        cards_at={
            "origin": {"target"},
            "host": {"origin", "target"},
            "target": {"origin"},
        }
    )
    result = await _hop(nodes[origin]).send(
        to_instance_id=target,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"content": "through-the-host"},
        space_id=SPACE,
    )
    await _settle()
    assert result.ok and result.via == "host_hop"
    [got] = nodes[target].dispatched
    assert got.from_instance == origin
    assert got.payload == {"content": "through-the-host"}
    assert got.space_id == SPACE and got.routed_path == [origin, host, target]
    # The host relayed only — it never saw the payload.
    assert nodes[host].dispatched == []
    assert "through-the-host" not in str(nodes[host].fed.sent)
    # Both legs carry the routing space id (the target's mailbox gate).
    assert {s["space_id"] for s in nodes[origin].fed.sent} == {SPACE}
    assert {s["space_id"] for s in nodes[host].fed.sent} == {SPACE}


async def test_the_host_drops_a_route_to_a_member_it_holds_no_card_of():
    nodes, (origin, host, target) = _wire(
        cards_at={"origin": {"target"}, "host": {"origin"}, "target": {"origin"}}
    )
    await _hop(nodes[origin]).send(
        to_instance_id=target,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"x": 1},
        space_id=SPACE,
    )
    await _settle()
    assert nodes[target].dispatched == []
    assert all(s["to"] != target for s in nodes[host].fed.sent)


async def test_the_target_drops_an_origin_it_holds_no_card_of():
    nodes, (origin, _host, target) = _wire(
        cards_at={"origin": {"target"}, "host": {"origin", "target"}}
    )
    await _hop(nodes[origin]).send(
        to_instance_id=target,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"x": 1},
        space_id=SPACE,
    )
    await _settle()
    assert nodes[target].dispatched == []


async def test_a_seat_cannot_use_a_relay_that_is_not_the_space_host():
    """The middle hop must be the routing space's host: a member that is
    not the host never relays for a seat."""
    nodes, (origin, mid, target) = _wire(
        cards_at={
            "origin": {"target"},
            "host": {"origin", "target"},
            "target": {"origin"},
        },
        host="someone-else",
    )
    await nodes[origin].handler.send_routed(
        path=[origin, mid, target],
        target_eph_pk_b64=b64url_encode(generate_x25519_keypair().public_key),
        inner_event_type=FederationEventType.SPACE_POST_CREATED,
        inner_payload={"x": 1},
        space_id=SPACE,
    )
    await _settle()
    assert all(s["to"] != target for s in nodes[mid].fed.sent)
    assert nodes[target].dispatched == []


async def test_a_seat_sending_a_longer_or_space_less_route_is_dropped():
    nodes, (origin, host, target) = _wire(
        cards_at={
            "origin": {"target"},
            "host": {"origin", "target"},
            "target": {"origin"},
        }
    )
    for path, space in (
        ([origin, host, target, "fourth-hop"], SPACE),
        ([origin, host, target], None),
    ):
        nodes[host].fed.sent.clear()
        await nodes[origin].handler.send_routed(
            path=path,
            target_eph_pk_b64=b64url_encode(generate_x25519_keypair().public_key),
            inner_event_type=FederationEventType.SPACE_POST_CREATED,
            inner_payload={"x": 1},
            space_id=space,
        )
        await _settle()
        assert nodes[host].fed.sent == []
    assert nodes[target].dispatched == []


async def test_a_paired_mesh_route_is_untouched():
    """No ``space_session`` seat on the route: the ordinary mesh rules,
    with no host-hop check at all."""
    nodes = _build_chain(["a", "b", "c"])
    for node in nodes.values():
        _carry_space_id(node)
    kp = generate_x25519_keypair()
    priv, pub = kp.private_key, kp.public_key
    nodes["c"].target_eph_store[b64url_encode(pub)] = b64url_encode(priv)
    await nodes["a"].handler.send_routed(
        path=["a", "b", "c"],
        target_eph_pk_b64=b64url_encode(pub),
        inner_event_type=FederationEventType.SPACE_POST_CREATED,
        inner_payload={"x": 1},
    )
    await _settle()
    assert len(nodes["c"].dispatched) == 1


async def test_no_host_hop_when_we_are_the_host_or_hold_no_card():
    async def no_card(_s, _i):
        return None

    async def host_is_me(_s):
        return "me"

    kw = generate_x25519_keypair()
    hop = HostHop(
        own_instance_id="me",
        card_of=no_card,
        host_of=host_is_me,
        keywrap_private_key=kw.private_key,
        keywrap_public_key=kw.public_key,
    )
    hop.attach_routed(SimpleNamespace())  # type: ignore[arg-type]
    result = await hop.send(
        to_instance_id="them",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={},
        space_id=SPACE,
    )
    assert not result.ok and result.error == HOST_HOP_UNAVAILABLE
    assert hop.static_target_priv(b64url_encode(kw.public_key)) == b64url_encode(
        kw.private_key
    )
    assert hop.static_target_priv("someone-else") is None


async def test_the_host_hop_never_widens_a_seats_vocabulary():
    """Review C1: a ``space_session`` seat must not deliver, through the
    host, an inner event it could not send directly (a DM here) — not at
    the host, and not at the target either."""
    nodes, (origin, host, target) = _wire(
        cards_at={
            "origin": {"target"},
            "host": {"origin", "target"},
            "target": {"origin"},
        }
    )
    result = await _hop(nodes[origin]).send(
        to_instance_id=target,
        event_type=FederationEventType.DM_MESSAGE,
        payload={"text": "not through the hop"},
        space_id=SPACE,
    )
    await _settle()
    assert result.ok  # the origin cannot tell; the host drops it
    assert all(s["to"] != target for s in nodes[host].fed.sent)
    assert nodes[target].dispatched == []
    hop = _hop(nodes[target])
    assert not await hop.allows(
        space_id=SPACE,
        path=[origin, host, target],
        inner_type=FederationEventType.DM_MESSAGE,
    )
    assert await hop.allows(
        space_id=SPACE,
        path=[origin, host, target],
        inner_type=FederationEventType.SPACE_POST_CREATED,
    )


async def test_an_inner_payload_naming_another_space_is_dropped():
    """Review I6: the routing space id is re-signed by the host on its
    leg; a payload that names another space than the route is dropped."""
    nodes, (origin, _host, target) = _wire(
        cards_at={
            "origin": {"target"},
            "host": {"origin", "target"},
            "target": {"origin"},
        }
    )
    await _hop(nodes[origin]).send(
        to_instance_id=target,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"space_id": "another-space", "x": 1},
        space_id=SPACE,
    )
    await _settle()
    assert nodes[target].dispatched == []


async def test_a_directory_seated_origin_keeps_its_vocabulary_on_a_mesh_route():
    """Review round 1 (#1336): an origin we hold only as a ``space_session``
    seat may not deliver, over an ordinary paired mesh route (no seat hop),
    an inner event outside the seat vocabulary."""
    nodes = _build_chain(["a", "b", "c"])
    for node in nodes.values():
        _carry_space_id(node)
    nodes["c"].repo._instances["a"] = _FakeInstance(
        id="a", source=InstanceSource.SPACE_SESSION
    )
    kp = generate_x25519_keypair()
    nodes["c"].target_eph_store[b64url_encode(kp.public_key)] = b64url_encode(
        kp.private_key
    )
    for inner, expected in (
        (FederationEventType.DM_MESSAGE, 0),
        (FederationEventType.SPACE_POST_CREATED, 1),
    ):
        nodes["c"].dispatched.clear()
        await nodes["a"].handler.send_routed(
            path=["a", "b", "c"],
            target_eph_pk_b64=b64url_encode(kp.public_key),
            inner_event_type=inner,
            inner_payload={"x": 1},
        )
        await _settle()
        assert len(nodes["c"].dispatched) == expected


def _route_stale_sends(node: _Node) -> list[dict]:
    return [
        s
        for s in node.fed.sent
        if s["event_type"] is FederationEventType.SPACE_ROUTE_STALE
    ]


async def test_a_paired_target_without_the_origins_card_sends_no_route_stale():
    """A host hop is sealed to the target's static key-wrap key, never to a
    discovered ephemeral: a target that does not take it (no card for the
    origin yet) has nothing stale to report. It drops the envelope and
    sends no ``SPACE_ROUTE_STALE`` — whose way back ends at a seat, which
    never rediscovers a route and does not take one."""
    nodes, (origin, host, target) = _wire(
        cards_at={"origin": {"target"}, "host": {"origin", "target"}}
    )
    # The target is PAIRED with the host (the demo's b <-> a), so the
    # seat-shape check on receipt does not apply: the unwrap decides.
    nodes[target].repo._instances[host] = _FakeInstance(id=host)
    result = await _hop(nodes[origin]).send(
        to_instance_id=target,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"x": 1},
        space_id=SPACE,
    )
    await _settle()
    assert result.ok
    assert nodes[target].dispatched == []
    assert _route_stale_sends(nodes[target]) == []


async def test_a_target_without_the_origins_card_asks_its_host_for_the_directory():
    """The card is missing because our directory is (a lost first
    snapshot): the target asks the space's host for it at once, so the
    origin's next host hop lands — the drop is not silent forever."""
    nodes, (origin, host, target) = _wire(
        cards_at={"origin": {"target"}, "host": {"origin", "target"}}
    )
    nodes[target].repo._instances[host] = _FakeInstance(id=host)
    asked: list[str] = []

    async def ask(space_id: str) -> None:
        asked.append(space_id)

    _hop(nodes[target])._ask_host = ask
    _hop(nodes[host])._ask_host = ask  # the host never asks itself
    await _hop(nodes[origin]).send(
        to_instance_id=target,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"x": 1},
        space_id=SPACE,
    )
    await _settle()
    assert asked == [SPACE]


async def test_a_target_never_nacks_a_seat_as_previous_hop():
    """The host-hop shape sealed to a key the target does not hold (not
    its key-wrap key): the previous hop is a seat of the target, so no
    nack goes out — a seat's vocabulary has no ``SPACE_ROUTE_STALE``."""
    nodes, (origin, host, target) = _wire(
        cards_at={
            "origin": {"target"},
            "host": {"origin", "target"},
            "target": {"origin"},
        }
    )
    await nodes[origin].handler.send_routed(
        path=[origin, host, target],
        target_eph_pk_b64=b64url_encode(generate_x25519_keypair().public_key),
        inner_event_type=FederationEventType.SPACE_POST_CREATED,
        inner_payload={"x": 1},
        space_id=SPACE,
    )
    await _settle()
    assert nodes[target].dispatched == []
    assert _route_stale_sends(nodes[target]) == []
    # And a seat never takes one either: it stays out of its vocabulary.
    assert (
        FederationEventType.SPACE_ROUTE_STALE not in SPACE_SESSION_ALLOWED_EVENT_TYPES
    )

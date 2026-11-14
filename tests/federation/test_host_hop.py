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
    DELIVERY_ERROR_QUEUED,
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


def _wire(
    *,
    cards_at: dict[str, set[str]],
    host: str | None = None,
    serves: set[str] | None = None,
):
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
    space_hosts = [host or mid]
    serving = set(space_hosts) if serves is None else serves
    role = {origin: "origin", mid: "host", target: "target"}
    for node_id, node in nodes.items():
        _carry_space_id(node)
        holds = cards_at.get(role[node_id], set())

        async def card_of(space_id, iid, holds=holds):
            if space_id != SPACE or iid not in role:
                return None
            return cards[iid] if role[iid] in holds else None

        async def acting(space_id, node_id=node_id):
            # The households acting as host for this node (never itself).
            if space_id != SPACE:
                return []
            return [h for h in space_hosts if h != node_id]

        async def hosts_of(space_id, _target, acting=acting):
            # Every target here takes the same hosts.
            return await acting(space_id)

        async def serves(space_id, node_id=node_id):
            return space_id == SPACE and node_id in serving

        hop = HostHop(
            own_instance_id=node_id,
            card_of=card_of,
            hosts_of=hosts_of,
            acting_hosts=acting,
            serves=serves,
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

    async def no_other_host(_s, _t=None):
        return []  # we are the host: nobody else to hop through

    async def we_serve(_s):
        return True

    kw = generate_x25519_keypair()
    hop = HostHop(
        own_instance_id="me",
        card_of=no_card,
        hosts_of=no_other_host,
        acting_hosts=no_other_host,
        serves=we_serve,
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


# ── "Host" = the owner or the delegated admin that invited us ────────────


_ALL_CARDS = {
    "origin": {"target"},
    "host": {"origin", "target"},
    "target": {"origin"},
}


async def _send_through(nodes, origin, target) -> None:
    await _hop(nodes[origin]).send(
        to_instance_id=target,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"x": 1},
        space_id=SPACE,
    )
    await _settle()


async def test_a_household_that_does_not_act_as_host_never_relays():
    """Both ends name it as their host, it holds both cards — but it is
    neither the owner nor a delegated admin holding the seed (delegation
    went off, it was demoted): it relays nothing."""
    nodes, (origin, _mid, target) = _wire(cards_at=_ALL_CARDS, serves=set())
    await _send_through(nodes, origin, target)
    assert nodes[target].dispatched == []


async def test_an_end_takes_the_hop_only_through_a_household_acting_as_its_host():
    """The hop serves (a delegated admin) and the origin takes it as host
    (it invited the origin) — the target does not (the owner invited it):
    a routed envelope through that admin is dropped at the target."""
    nodes, (origin, _mid, target) = _wire(cards_at=_ALL_CARDS)

    async def owner_only(_space_id):
        return ["the-owner"]

    _hop(nodes[target])._acting = owner_only
    await _send_through(nodes, origin, target)
    assert nodes[target].dispatched == []


async def test_the_hop_goes_through_the_first_host_that_is_not_an_end():
    sent: list[list[str]] = []

    class _Routed:
        async def send_routed(self, *, path, **_kw):
            sent.append(path)
            return "route"

    ids, idents = _identity_chain()
    target = ids[0]
    kw = generate_x25519_keypair()
    card = build_card(
        instance_id=target,
        identity_seed=idents[target].private_key,
        identity_pk=idents[target].public_key,
        keywrap_pk=kw.public_key,
        keywrap_sig=b64url_encode(
            sign_ed25519(idents[target].private_key, kw.public_key)
        ),
        proto_version=89,
        rev=1,
        mailbox=None,
    )

    async def card_of(_s, iid):
        return card if iid == target else None

    async def hosts_of(_s, _t):
        # The owner IS the target here: the admin that invited us is next.
        return [target, "the-admin"]

    async def acting(_s):
        return [target, "the-admin"]

    async def serves(_s):
        return False

    hop = HostHop(
        own_instance_id="me",
        card_of=card_of,
        hosts_of=hosts_of,
        acting_hosts=acting,
        serves=serves,
        keywrap_private_key=kw.private_key,
        keywrap_public_key=kw.public_key,
    )
    hop.attach_routed(_Routed())  # type: ignore[arg-type]
    result = await hop.send(
        to_instance_id=target,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={},
        space_id=SPACE,
    )
    assert result.ok and sent == [["me", "the-admin", target]]


async def test_no_hop_the_target_would_refuse_is_reported_as_sent():
    """Review round 1 (#1361): we take the admin as host, the target does
    not (the owner invited it) and we hold no link to the owner — there is
    no hop the target takes. The send FAILS; it never returns ``ok`` for an
    envelope the target drops."""
    sent: list[list[str]] = []

    class _Routed:
        async def send_routed(self, *, path, **_kw):
            sent.append(path)
            return "route"

    kw = generate_x25519_keypair()

    async def card_of(_s, _i):
        raise AssertionError("no card is read when there is no hop")

    async def no_hop_for_this_target(_s, _t):
        return []

    async def acting(_s):
        return ["the-owner", "the-admin"]

    async def serves(_s):
        return False

    hop = HostHop(
        own_instance_id="me",
        card_of=card_of,
        hosts_of=no_hop_for_this_target,
        acting_hosts=acting,
        serves=serves,
        keywrap_private_key=kw.private_key,
        keywrap_public_key=kw.public_key,
    )
    hop.attach_routed(_Routed())  # type: ignore[arg-type]
    result = await hop.send(
        to_instance_id="them",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={},
        space_id=SPACE,
    )
    assert not result.ok and result.error == HOST_HOP_UNAVAILABLE
    assert sent == []


# ── Review round 2 (#1361): the hops are tried in order ──────────────────


def _hop_with(results: dict[str, Any], hosts: list[str]):
    """A hop whose routed first leg toward each host answers *results*."""
    sent: list[str] = []
    ids, idents = _identity_chain()
    target = ids[0]
    kw = generate_x25519_keypair()
    card = build_card(
        instance_id=target,
        identity_seed=idents[target].private_key,
        identity_pk=idents[target].public_key,
        keywrap_pk=kw.public_key,
        keywrap_sig=b64url_encode(
            sign_ed25519(idents[target].private_key, kw.public_key)
        ),
        proto_version=89,
        rev=1,
        mailbox=None,
    )

    class _Routed:
        async def send_routed(self, *, path, first_leg, **_kw):
            sent.append(path[1])
            first_leg.append(results[path[1]])
            return "route"

    async def card_of(_s, _i):
        return card

    async def hosts_of(_s, _t):
        return hosts

    async def acting(_s):
        return hosts

    async def serves(_s):
        return False

    hop = HostHop(
        own_instance_id="me",
        card_of=card_of,
        hosts_of=hosts_of,
        acting_hosts=acting,
        serves=serves,
        keywrap_private_key=kw.private_key,
        keywrap_public_key=kw.public_key,
    )
    hop.attach_routed(_Routed())  # type: ignore[arg-type]

    async def send():
        return await hop.send(
            to_instance_id=target,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload={},
            space_id=SPACE,
        )

    return send, sent


def _leg(ok: bool, error: str | None = None):
    return SimpleNamespace(ok=ok, error=error)


async def test_a_hop_that_is_unreachable_now_gives_way_to_the_next():
    """The owner is offline (the envelope only sits in our outbox for it):
    the admin that seated both ends is online and takes it."""
    send, sent = _hop_with(
        {"owner": _leg(False, DELIVERY_ERROR_QUEUED), "admin": _leg(True)},
        ["owner", "admin"],
    )
    result = await send()
    assert result.ok and result.via == "host_hop"
    assert sent == ["owner", "admin"]


async def test_the_first_hop_that_takes_it_is_the_only_one_used():
    send, sent = _hop_with(
        {"owner": _leg(True), "admin": _leg(True)}, ["owner", "admin"]
    )
    assert (await send()).ok
    assert sent == ["owner"]


async def test_no_hop_took_it_is_never_reported_as_delivered():
    send, sent = _hop_with(
        {"owner": _leg(False, DELIVERY_ERROR_QUEUED), "admin": _leg(False, "nope")},
        ["owner", "admin"],
    )
    result = await send()
    # Queued for the owner: it redelivers on its own — said as such.
    assert not result.ok and result.error == DELIVERY_ERROR_QUEUED
    assert sent == ["owner", "admin"]
    send, _sent = _hop_with({"admin": _leg(False, "nope")}, ["admin"])
    result = await send()
    assert not result.ok and result.error == HOST_HOP_UNAVAILABLE

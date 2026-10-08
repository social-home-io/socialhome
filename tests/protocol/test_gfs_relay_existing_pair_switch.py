"""§27.9 release blocker: the GFS fallback switch for an existing pair (v_54).

Two households paired with a plain ``url`` code: neither opted into the
relay and neither holds the other's key-wrap key. Each admin turns the
GFS fallback on for the other through
:class:`~socialhome.services.peer_gfs_relay_service.PeerGfsRelayService`.
Routes may form only once BOTH have: the key-wrap keys travel inside the
encrypted ``INSTANCE_CAPABILITIES_UPDATED`` payload, the receiver checks
them against the identity key it pinned at pairing, and route discovery
then finds the one server the two share.

Real crypto, real SQLite, the real §24.11 pipeline (HTTPS-inbox and relay
legs), the real capabilities sender and receiver, the real switch and the
real route discovery. The stand-ins are the network: a loopback HTTPS inbox
between the two households, and fake GFSes that push a ``POST /gfs/envelope``
body down the addressed household's socket iff it is connected there.

Topology: household **a** uses GFS {X, Y}, household **b** uses {Y, Z}.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from socialhome.app import _build_gfs_route_resolver
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    FederationEventType,
    GfsConnection,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import OURS
from socialhome.federation.federation_service import FederationService
from socialhome.federation.gfs_relay_transport import GfsRelayTransport
from socialhome.federation.transport import FederationTransport
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.services.capabilities_outbound import CapabilitiesOutbound
from socialhome.services.federation_inbound import PairingInboundHandlers
from socialhome.services.gfs_relay_inbound import GfsRelayInbound
from socialhome.services.gfs_route_discovery_service import GfsRouteDiscoveryService
from socialhome.services.peer_gfs_relay_service import PeerGfsRelayService

pytestmark = pytest.mark.security

URL_X = "https://gfs-x.example.org"
URL_Y = "https://gfs-y.example.org"
URL_Z = "https://gfs-z.example.org"


class _Gfs:
    """One connection server: stores every body, pushes to connected sockets."""

    def __init__(self, url: str, tasks: set[asyncio.Task]) -> None:
        self.url = url
        self.sockets: dict[str, GfsRelayInbound] = {}
        self.bodies: list[dict] = []
        self._tasks = tasks

    def post(self, body: dict) -> None:
        self.bodies.append(body)
        target = self.sockets.get(body["to_instance"])
        if target is None:
            return
        task = asyncio.create_task(
            target.handle_frame({"sealed": body["sealed"]}, gfs_url=self.url),
        )
        self._tasks.add(task)


class _Sender:
    """A household's ``RelayEnvelopeSender``: only its own servers."""

    def __init__(self, net: dict[str, _Gfs], own: set[str]) -> None:
        self._net = net
        self._own = own

    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        if gfs_url not in self._own:
            return False
        self._net[gfs_url].post(
            {"to_instance": to_instance_id, "sealed": envelope["sealed"]},
        )
        return True


class _LoopbackInbox:
    """The HTTPS inbox leg: POSTs the envelope straight into the peer's
    §24.11 inbox pipeline, recording what crossed the wire."""

    def __init__(self, world: dict[str, SimpleNamespace], me: str) -> None:
        self._world = world
        self._me = me
        self.posted: list[dict] = []
        #: The peer's inbox is unreachable (network error, no status).
        self.down = False
        #: Accept but deliver later (a slow link): see :meth:`release`.
        self.hold = False
        self._held: list[tuple] = []

    async def release(self) -> None:
        held, self._held = self._held, []
        self.hold = False
        for instance, envelope_dict in held:
            await self.send(instance=instance, envelope_dict=envelope_dict)

    async def send(self, *, instance, envelope_dict):
        if self.down:
            return False, None
        if self.hold:
            self._held.append((instance, envelope_dict))
            return True, 200
        peer = next(h for h in self._world.values() if h.iid == instance.id)
        me = self._world[self._me]
        self.posted.append(envelope_dict)
        try:
            await peer.federation.handle_inbound_envelope(
                f"{peer.name}-inbox-for-{me.name}",
                json.dumps(envelope_dict).encode(),
            )
        except ValueError:
            return False, 403
        return True, 200


class _NoBootstrap:
    async def handle_bootstrap_body(self, body, *, gfs_url=""):
        raise ValueError("no bootstrap in this test")


class _NoUnpair:
    async def forget(self, *_a, **_kw):  # pragma: no cover — never called
        raise AssertionError("no unpair in this test")


async def _no_signal(*_a, **_kw):
    raise RuntimeError("no RTC signalling here")


async def _household(tmp_path, name, net, urls, world, tasks):
    db = AsyncDatabase(tmp_path / f"{name}.db", batch_timeout_ms=10)
    await db.startup()
    ident = generate_identity_keypair()
    iid = derive_instance_id(ident.public_key)
    keywrap = generate_x25519_keypair()
    km = KeyManager(os.urandom(32))
    bus = EventBus()
    fed_repo = SqliteFederationRepo(db)
    gfs_repo = SqliteGfsConnectionRepo(db)
    outbox = SqliteOutboxRepo(db)
    federation = FederationService(
        db,
        fed_repo,
        outbox,
        km,
        bus,
        iid,
        ident.private_key,
        ident.public_key,
    )
    conn_ids: dict[str, str] = {}
    for url in urls:
        cid = f"{name}-conn-{url.split('//')[1][:5]}"
        conn_ids[url] = cid
        await gfs_repo.save(
            GfsConnection(
                id=cid,
                gfs_instance_id=f"gfs-instance-{url[-15:]}",
                display_name=f"server {url}",
                public_key="ab" * 32,
                inbox_url=url,
                status="active",
                paired_at="2026-01-01T00:00:00+00:00",
            ),
        )
    inbox = _LoopbackInbox(world, name)
    transport = FederationTransport(
        own_instance_id=iid,
        https_inbox=inbox,
        gfs_relay=GfsRelayTransport(relay_sender=_Sender(net, set(urls))),
        gfs_routes=_build_gfs_route_resolver(
            federation_repo=fed_repo,
            gfs_connection_repo=gfs_repo,
        ),
        signaling_send=_no_signal,
    )
    transport.mark_ice_primed()
    federation.attach_transport(transport)
    relay_inbound = GfsRelayInbound(
        federation=federation,
        keywrap_private_key=keywrap.private_key,
        invite_coordinator=_NoBootstrap(),
        gfs_connection_repo=gfs_repo,
    )
    for url in urls:
        net[url].sockets[iid] = relay_inbound

    async def _relay_capable(_conn) -> bool:
        return True

    discovery = GfsRouteDiscoveryService(
        federation=federation,
        federation_repo=fed_repo,
        gfs_connection_repo=gfs_repo,
        envelope_relay_supported=_relay_capable,
    )
    discovery.attach_to(federation)
    PairingInboundHandlers(
        bus=bus,
        federation_repo=fed_repo,
        peer_unpair=_NoUnpair(),
    ).attach_to(federation)
    capabilities = CapabilitiesOutbound(
        federation_service=federation,
        federation_repo=fed_repo,
        bus=bus,
        keywrap_public_key=keywrap.public_key,
        keywrap_sig=b64url_encode(sign_ed25519(ident.private_key, keywrap.public_key)),
    )
    switch = PeerGfsRelayService(
        federation_repo=fed_repo,
        send_capabilities=capabilities.resend_to,
        probe_peer=discovery.probe_peer,
        forget_probes=discovery.forget_peer,
        bus=bus,
    )
    switch.wire()
    h = SimpleNamespace(
        name=name,
        db=db,
        outbox=outbox,
        iid=iid,
        ident=ident,
        keywrap=keywrap,
        km=km,
        fed_repo=fed_repo,
        federation=federation,
        transport=transport,
        inbox=inbox,
        discovery=discovery,
        switch=switch,
        conn_ids=conn_ids,
    )
    world[name] = h
    return h


async def _url_pair(a, b):
    """A classic ``url`` pairing: an address each, no relay opt-in, no
    key-wrap key on either side."""
    k_ab, k_ba = os.urandom(32), os.urandom(32)
    for me, peer, k_out, k_in in ((a, b, k_ab, k_ba), (b, a, k_ba, k_ab)):
        await me.fed_repo.save_instance(
            RemoteInstance(
                id=peer.iid,
                display_name=f"{peer.name} household",
                remote_identity_pk=peer.ident.public_key.hex(),
                key_self_to_remote=me.km.encrypt(k_out),
                key_remote_to_self=me.km.encrypt(k_in),
                remote_inbox_url=f"https://{peer.name}.example/federation/inbox/x",
                local_inbox_id=f"{me.name}-inbox-for-{peer.name}",
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
            ),
        )
        await me.fed_repo.set_proto_version(peer.iid, OURS)
        me.transport._rtc_suppressed_until[peer.iid] = float("inf")
    return k_ab, k_ba


@pytest.fixture
async def world(tmp_path):
    tasks: set[asyncio.Task] = set()
    net: dict[str, _Gfs] = {u: _Gfs(u, tasks) for u in (URL_X, URL_Y, URL_Z)}
    households: dict[str, SimpleNamespace] = {}
    a = await _household(tmp_path, "a", net, [URL_X, URL_Y], households, tasks)
    b = await _household(tmp_path, "b", net, [URL_Y, URL_Z], households, tasks)

    async def drain() -> None:
        while tasks:
            batch = list(tasks)
            tasks.clear()
            await asyncio.gather(*batch, return_exceptions=True)

    yield SimpleNamespace(a=a, b=b, net=net, drain=drain)
    await drain()
    await a.db.shutdown()
    await b.db.shutdown()


async def _route_ids(me, peer) -> list[str]:
    return [r.gfs_connection_id for r in await me.fed_repo.list_gfs_routes(peer.iid)]


def _relay_bodies(world) -> list[dict]:
    return [body for gfs in world.net.values() for body in gfs.bodies]


async def test_only_one_side_switched_on_means_no_relay_traffic(world):
    a, b = world.a, world.b
    await _url_pair(a, b)

    await a.switch.set_gfs_relay(b.iid, enabled=True)
    await world.drain()

    # b now holds a's key (verified), but b did not opt in…
    row_b = await b.fed_repo.get_instance(a.iid)
    assert row_b.remote_keywrap_pk == a.keywrap.public_key.hex()
    assert row_b.gfs_relay is False
    # …and a holds no key for b, so a cannot seal anything to it: no
    # probe, no relay body anywhere, no route on either side.
    assert (await a.fed_repo.get_instance(b.iid)).remote_keywrap_pk is None
    assert _relay_bodies(world) == []
    assert await _route_ids(a, b) == []
    assert await _route_ids(b, a) == []


async def test_both_sides_switched_on_find_the_shared_gfs(world):
    a, b = world.a, world.b
    await _url_pair(a, b)

    await a.switch.set_gfs_relay(b.iid, enabled=True)
    await world.drain()
    await b.switch.set_gfs_relay(a.iid, enabled=True)
    await world.drain()

    # Keys exchanged both ways, each verified against the pinned identity.
    assert (await a.fed_repo.get_instance(b.iid)).remote_keywrap_pk == (
        b.keywrap.public_key.hex()
    )
    assert (await b.fed_repo.get_instance(a.iid)).remote_keywrap_pk == (
        a.keywrap.public_key.hex()
    )
    # Only the shared server Y became a route — on both sides, each its own
    # connection id.
    assert await _route_ids(a, b) == [a.conn_ids[URL_Y]]
    assert await _route_ids(b, a) == [b.conn_ids[URL_Y]]


async def test_switching_back_on_within_a_minute_routes_both_ways(world):
    """Keys already held (the switch was on before, or a GFS-reach pair):
    a switches on and probes at once — b refuses it, b is not on yet —
    and b switches on seconds later. b's probe reaches a, and a's probe
    back must not be swallowed by the per-peer probe throttle the admin's
    own switch just started, or a would hold no route for a day."""
    a, b = world.a, world.b
    await _url_pair(a, b)
    for me, peer in ((a, b), (b, a)):
        await me.fed_repo.set_remote_keywrap_pk(peer.iid, peer.keywrap.public_key.hex())

    await a.switch.set_gfs_relay(b.iid, enabled=True)
    await world.drain()
    assert await _route_ids(a, b) == []
    await b.switch.set_gfs_relay(a.iid, enabled=True)
    await world.drain()

    assert await _route_ids(a, b) == [a.conn_ids[URL_Y]]
    assert await _route_ids(b, a) == [b.conn_ids[URL_Y]]


async def test_the_keywrap_key_travels_only_inside_the_encrypted_payload(world):
    a, b = world.a, world.b
    k_ab, _k_ba = await _url_pair(a, b)

    await a.switch.set_gfs_relay(b.iid, enabled=True)
    await world.drain()

    caps = [
        env
        for env in a.inbox.posted
        if env["event_type"] == FederationEventType.INSTANCE_CAPABILITIES_UPDATED.value
    ]
    assert len(caps) == 1
    clear = json.dumps(caps[0])
    assert a.keywrap.public_key.hex() not in clear
    assert "keywrap" not in clear
    plain = json.loads(
        a.federation._decrypt_payload(caps[0]["encrypted_payload"], k_ab),
    )
    assert plain["keywrap_pk"] == a.keywrap.public_key.hex()
    assert plain["keywrap_suite"] == "x25519"


async def test_switching_off_drops_the_routes_and_refuses_relayed_traffic(world):
    a, b = world.a, world.b
    await _url_pair(a, b)
    await a.switch.set_gfs_relay(b.iid, enabled=True)
    await world.drain()
    await b.switch.set_gfs_relay(a.iid, enabled=True)
    await world.drain()
    assert await _route_ids(a, b) == [a.conn_ids[URL_Y]]

    await a.switch.set_gfs_relay(b.iid, enabled=False)
    await world.drain()
    assert await _route_ids(a, b) == []
    # a told b (``gfs_relay: false`` in its encrypted capabilities): b drops
    # its routes to a at once instead of relaying into a's closed gate for
    # up to 72 h. b's own switch stays on — that is b's admin's call.
    assert await _route_ids(b, a) == []
    assert (await b.fed_repo.get_instance(a.iid)).gfs_relay is True

    # b's next send to a, with a's inbox unreachable, is queued in b's
    # outbox for a later retry — not handed to a relay that would answer
    # 202 while a refuses it (counted delivered, silently lost).
    b.inbox.down = True
    before_y = len(world.net[URL_Y].bodies)
    result = await b.federation.send_event(
        to_instance_id=a.iid,
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={"username": "bea", "state": "home"},
    )
    await world.drain()
    assert result.via != "gfs_relay"
    assert len(world.net[URL_Y].bodies) == before_y
    assert await b.outbox.count_pending_for(a.iid) == 1
    b.inbox.down = False

    # b still relays a probe through Y — a's §24.11
    # relay opt-in gate drops it before any handler runs: no ack, no new
    # route on a's side.
    b.discovery._last_probe_at.clear()
    before = len(world.net[URL_Y].bodies)
    await b.discovery.probe_peer(a.iid)
    await world.drain()
    to_b = [
        body
        for body in world.net[URL_Y].bodies[before:]
        if body["to_instance"] == b.iid
    ]
    assert to_b == []
    assert await _route_ids(a, b) == []


async def test_a_switch_on_probe_that_overtakes_the_key_still_ends_in_routes(world):
    """b switches on; its capabilities (with its key) travel a slow direct
    link while its probe races ahead through the GFS. a cannot seal an ack
    yet — and must not let that unanswerable probe throttle b's next one."""
    a, b = world.a, world.b
    await _url_pair(a, b)
    await a.switch.set_gfs_relay(b.iid, enabled=True)
    await world.drain()

    b.inbox.hold = True
    await b.switch.set_gfs_relay(a.iid, enabled=True)
    await world.drain()
    assert await _route_ids(a, b) == []
    assert await _route_ids(b, a) == []

    await b.inbox.release()
    await world.drain()

    assert await _route_ids(a, b) == [a.conn_ids[URL_Y]]
    assert await _route_ids(b, a) == [b.conn_ids[URL_Y]]


async def test_switching_back_on_restores_the_routes_on_both_sides(world):
    a, b = world.a, world.b
    await _url_pair(a, b)
    await a.switch.set_gfs_relay(b.iid, enabled=True)
    await world.drain()
    await b.switch.set_gfs_relay(a.iid, enabled=True)
    await world.drain()
    await a.switch.set_gfs_relay(b.iid, enabled=False)
    await world.drain()
    assert await _route_ids(b, a) == []

    await a.switch.set_gfs_relay(b.iid, enabled=True)
    await world.drain()

    assert await _route_ids(a, b) == [a.conn_ids[URL_Y]]
    assert await _route_ids(b, a) == [b.conn_ids[URL_Y]]


async def test_a_key_not_signed_by_the_paired_identity_is_never_stored(world):
    """b's capabilities claim a key-wrap key that b's identity did not
    sign (what a relay that wanted to read our sealed envelopes would
    send). a keeps no key, so a's switch can never seal to it."""
    a, b = world.a, world.b
    await _url_pair(a, b)
    await a.fed_repo.set_gfs_relay(b.iid, enabled=True)
    impostor = generate_x25519_keypair()

    await b.federation.send_event(
        to_instance_id=a.iid,
        event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        payload={
            "proto_version": OURS,
            "keywrap_pk": impostor.public_key.hex(),
            "keywrap_sig": b64url_encode(
                sign_ed25519(
                    generate_identity_keypair().private_key, impostor.public_key
                ),
            ),
            "keywrap_suite": "x25519",
        },
    )
    await world.drain()

    assert (await a.fed_repo.get_instance(b.iid)).remote_keywrap_pk is None
    assert _relay_bodies(world) == []

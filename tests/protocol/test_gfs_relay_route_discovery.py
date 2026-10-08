"""§27.9 release blocker: shared-GFS route discovery (v_53).

Two paired households that opted into the relay with each other find the
connection servers (GFSes) they BOTH use — without either one naming a
server to the other, and without any server learning more than it does
from any other relayed blob.

Real crypto, real SQLite, the real §24.11 pipeline, the real relay inbound
leg and the real transport facade. The only stand-in is the network: each
fake GFS pushes a ``POST /gfs/envelope`` body down the addressed
household's socket iff that household is connected to it, and each
household's relay sender may only post to its OWN servers (production's
``GfsEnvelopeSender`` resolves the URL against its own connections).

Topology: household **a** uses GFS {X, Y}, household **b** uses {Y, Z}.
Only Y may become a route — on both sides, each stored as that side's own
connection id.

Also the binding-rule test for the inbound tripwire
(``test_inbound_binding_tripwire.py``): both handlers write only
``peer_gfs_routes`` rows keyed on the SENDER's own instance id.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from socialhome.app import _build_gfs_route_resolver
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
    GfsConnection,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation.federation_service import FederationService
from socialhome.federation.gfs_relay_transport import GfsRelayTransport
from socialhome.federation.transport import FederationTransport
from socialhome.global_server.envelope_relay import SEALED_KEYS
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.services.gfs_relay_inbound import GfsRelayInbound
from socialhome.services.gfs_route_discovery_service import GfsRouteDiscoveryService

pytestmark = pytest.mark.security

URL_X = "https://gfs-x.example.org"
URL_Y = "https://gfs-y.example.org"
URL_Z = "https://gfs-z.example.org"


class _Gfs:
    """One connection server: stores every body, pushes to connected sockets."""

    def __init__(self, url: str, tasks: set[asyncio.Task]) -> None:
        self.url = url
        self.sockets: dict[str, GfsRelayInbound] = {}
        #: What a curious operator of this server would have in its logs.
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
        self.foreign: list[str] = []

    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        if gfs_url not in self._own:
            self.foreign.append(gfs_url)
            return False
        self._net[gfs_url].post(
            {"to_instance": to_instance_id, "sealed": envelope["sealed"]},
        )
        return True


class _RecordingRelay(GfsRelayTransport):
    """The real relay tier, recording every §24.11 envelope it seals."""

    def __init__(self, *, relay_sender, log: list) -> None:
        super().__init__(relay_sender=relay_sender)
        self.log = log

    async def send(self, *, instance, envelope_dict, gfs_url=None):
        self.log.append((gfs_url, envelope_dict))
        return await super().send(
            instance=instance, envelope_dict=envelope_dict, gfs_url=gfs_url
        )


class _DeadInbox:
    async def send(self, *, instance, envelope_dict):
        return False, None


class _NoBootstrap:
    async def handle_bootstrap_body(self, body, *, gfs_url=""):
        raise ValueError("no bootstrap in this test")


async def _no_signal(*_a, **_kw):
    raise RuntimeError("no RTC signalling here")


async def _household(tmp_path, name, net, urls: list[str], tasks):
    db = AsyncDatabase(tmp_path / f"{name}.db", batch_timeout_ms=10)
    await db.startup()
    ident = generate_identity_keypair()
    iid = derive_instance_id(ident.public_key)
    keywrap = generate_x25519_keypair()
    km = KeyManager(os.urandom(32))
    fed_repo = SqliteFederationRepo(db)
    gfs_repo = SqliteGfsConnectionRepo(db)
    federation = FederationService(
        db,
        fed_repo,
        SqliteOutboxRepo(db),
        km,
        EventBus(),
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
    sender = _Sender(net, set(urls))
    sealed_log: list = []
    transport = FederationTransport(
        own_instance_id=iid,
        https_inbox=_DeadInbox(),
        gfs_relay=_RecordingRelay(relay_sender=sender, log=sealed_log),
        gfs_routes=_build_gfs_route_resolver(
            federation_repo=fed_repo,
            gfs_connection_repo=gfs_repo,
        ),
        signaling_send=_no_signal,
    )
    transport.mark_ice_primed()
    federation.attach_transport(transport)
    inbound = GfsRelayInbound(
        federation=federation,
        keywrap_private_key=keywrap.private_key,
        invite_coordinator=_NoBootstrap(),
        gfs_connection_repo=gfs_repo,
    )
    for url in urls:
        net[url].sockets[iid] = inbound

    async def _relay_capable(_conn) -> bool:
        return True

    discovery = GfsRouteDiscoveryService(
        federation=federation,
        federation_repo=fed_repo,
        gfs_connection_repo=gfs_repo,
        envelope_relay_supported=_relay_capable,
    )
    discovery.attach_to(federation)
    received: list[FederationEvent] = []

    async def _capture(event: FederationEvent) -> None:
        received.append(event)

    federation._event_registry.register(
        FederationEventType.PRESENCE_UPDATED,
        _capture,
    )
    return SimpleNamespace(
        name=name,
        db=db,
        iid=iid,
        ident=ident,
        keywrap=keywrap,
        km=km,
        fed_repo=fed_repo,
        federation=federation,
        transport=transport,
        sender=sender,
        sealed_log=sealed_log,
        discovery=discovery,
        conn_ids=conn_ids,
        received=received,
    )


async def _pair(
    a, b, *, proto_version: int = FederationCapability.MIN_FOR_GFS_RELAY_ROUTES
):
    k_ab, k_ba = os.urandom(32), os.urandom(32)
    for me, peer, k_out, k_in in ((a, b, k_ab, k_ba), (b, a, k_ba, k_ab)):
        await me.fed_repo.save_instance(
            RemoteInstance(
                id=peer.iid,
                display_name=f"{peer.name} household",
                remote_identity_pk=peer.ident.public_key.hex(),
                key_self_to_remote=me.km.encrypt(k_out),
                key_remote_to_self=me.km.encrypt(k_in),
                remote_inbox_url="",
                local_inbox_id=f"{me.name}-inbox-for-{peer.name}",
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
                remote_keywrap_pk=peer.keywrap.public_key.hex(),
                gfs_relay=True,
            ),
        )
        await me.fed_repo.set_gfs_relay(peer.iid, enabled=True)
        await me.fed_repo.set_proto_version(peer.iid, proto_version)
        me.transport._rtc_suppressed_until[peer.iid] = float("inf")
    return k_ab, k_ba


@pytest.fixture
async def world(tmp_path):
    tasks: set[asyncio.Task] = set()
    net: dict[str, _Gfs] = {u: _Gfs(u, tasks) for u in (URL_X, URL_Y, URL_Z)}
    a = await _household(tmp_path, "a", net, [URL_X, URL_Y], tasks)
    b = await _household(tmp_path, "b", net, [URL_Y, URL_Z], tasks)

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


def _all_bodies(world) -> list[dict]:
    return [body for gfs in world.net.values() for body in gfs.bodies]


def _forbidden(world) -> list[str]:
    """Every string naming a server or a connection, on either side."""
    names = [URL_X, URL_Y, URL_Z, "gfs-x", "gfs-y", "gfs-z", "gfs-instance"]
    for h in (world.a, world.b):
        names += list(h.conn_ids.values())
        names.append(f"{h.name}-inbox-for-")
    return names


async def test_only_the_shared_server_becomes_a_route_on_both_sides(world):
    a, b = world.a, world.b
    await _pair(a, b)

    sent = await a.discovery.probe_peer(b.iid)
    await world.drain()

    # a probed through BOTH of its own servers, nothing else.
    assert sent == 2
    assert {
        url for url, env in a.sealed_log if env["event_type"] == "gfs_relay_probe"
    } == {
        URL_X,
        URL_Y,
    }
    assert a.sender.foreign == [] and b.sender.foreign == []
    # Each side holds only a route its OWN probe proved: Y, on both sides.
    assert await _route_ids(b, a) == [b.conn_ids[URL_Y]]
    assert await _route_ids(a, b) == [a.conn_ids[URL_Y]]
    # b acked through Y (where a's probe arrived) — never a round-robin pick.
    assert [
        url for url, env in b.sealed_log if env["event_type"] == "gfs_relay_probe_ack"
    ] == [URL_Y]
    # Z only ever saw b's own probe for a, which nobody collects.
    assert {body["to_instance"] for body in world.net[URL_Z].bodies} == {a.iid}
    # The X / Z probes are still waiting (and will expire); Y's were consumed.
    assert a.discovery.pending_count == 1
    assert b.discovery.pending_count == 1


async def test_a_malicious_gfs_replaying_a_probe_elsewhere_creates_no_route(world):
    """X (a's server, malicious) re-posts every sealed blob for b onto Z —
    anyone may POST the identity-free body, and b never saw that blob, so
    the replay cache does not stop it. b must not learn a route through Z:
    a never reads Z, so relaying there would be a silent loss."""
    a, b = world.a, world.b
    await _pair(a, b)
    honest_post = world.net[URL_X].post

    def _replaying_post(body: dict) -> None:
        honest_post(body)
        if body["to_instance"] == b.iid:
            world.net[URL_Z].post(dict(body))

    world.net[URL_X].post = _replaying_post  # type: ignore[method-assign]

    await a.discovery.probe_peer(b.iid)
    await world.drain()

    assert b.conn_ids[URL_Z] not in await _route_ids(b, a)
    assert await _route_ids(b, a) == [b.conn_ids[URL_Y]]
    assert await _route_ids(a, b) == [a.conn_ids[URL_Y]]


async def test_discovery_runs_the_same_way_from_the_other_side(world):
    a, b = world.a, world.b
    await _pair(a, b)

    assert await b.discovery.probe_peer(a.iid) == 2
    await world.drain()

    assert {
        url for url, env in b.sealed_log if env["event_type"] == "gfs_relay_probe"
    } == {URL_Y, URL_Z}
    assert await _route_ids(a, b) == [a.conn_ids[URL_Y]]
    assert await _route_ids(b, a) == [b.conn_ids[URL_Y]]


async def test_nothing_on_any_wire_names_a_server_or_a_connection(world):
    a, b = world.a, world.b
    k_ab, k_ba = await _pair(a, b)
    await a.discovery.probe_peer(b.iid)
    await world.drain()

    forbidden = _forbidden(world)
    # What every GFS saw: the identity-free relay body, nothing more.
    bodies = _all_bodies(world)
    assert bodies
    for body in bodies:
        assert set(body) == {"to_instance", "sealed"}
        assert set(body["sealed"]) == set(SEALED_KEYS)
        blob = json.dumps(body)
        for name in forbidden:
            assert name not in blob, f"relay body names {name!r}"
    # What the peer saw: the signed §24.11 envelope (routing fields in the
    # clear) and its decrypted payload — a nonce and nothing else.
    for me, key in ((a, k_ab), (b, k_ba)):
        for _url, env in me.sealed_log:
            assert env["event_type"] in {"gfs_relay_probe", "gfs_relay_probe_ack"}
            assert env["space_id"] is None
            clear = json.dumps(env)
            plain = json.loads(
                me.federation._decrypt_payload(env["encrypted_payload"], key)
            )
            assert set(plain) == {"nonce"}
            assert len(plain["nonce"]) >= 22
            for name in forbidden:
                assert name not in clear, f"envelope names {name!r}"
                assert name not in json.dumps(plain), f"payload names {name!r}"


async def test_the_discovered_route_carries_ordinary_traffic(world):
    """The round-robin tier now has a route — and it is the shared server."""
    a, b = world.a, world.b
    await _pair(a, b)
    await a.discovery.probe_peer(b.iid)
    await world.drain()
    before = len(world.net[URL_Y].bodies)

    result = await a.federation.send_event(
        to_instance_id=b.iid,
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={"username": "anna", "state": "home"},
    )
    await world.drain()

    assert result.ok is True and result.via == "gfs_relay"
    assert len(world.net[URL_Y].bodies) == before + 1
    assert [e.payload["username"] for e in b.received] == ["anna"]


async def test_a_pre_v53_peer_is_never_probed(world):
    a, b = world.a, world.b
    await _pair(a, b, proto_version=FederationCapability.MIN_FOR_GFS_RELAY_ROUTES - 1)

    assert await a.discovery.probe_peer(b.iid) == 0
    await world.drain()

    assert _all_bodies(world) == []
    assert await _route_ids(a, b) == []


async def test_a_probe_to_a_peer_that_did_not_opt_in_is_refused_by_the_pipeline(world):
    a, b = world.a, world.b
    await _pair(a, b)
    await b.fed_repo.set_gfs_relay(a.iid, enabled=False)

    await a.discovery.probe_peer(b.iid)
    await world.drain()

    # It crossed Y, but b's §24.11 relay opt-in gate dropped it before any
    # handler ran: no route on either side, no ack.
    assert world.net[URL_Y].bodies
    assert await _route_ids(b, a) == []
    assert await _route_ids(a, b) == []
    assert b.sealed_log == []


def test_link_joined_households_may_not_send_probes_or_acks():
    """Probe / ack are paired-peer vocabulary: the §D2b peer-class gate
    refuses them from a household seated from an invite link."""
    assert FederationEventType.GFS_RELAY_PROBE not in SPACE_SESSION_ALLOWED_EVENT_TYPES
    assert (
        FederationEventType.GFS_RELAY_PROBE_ACK not in SPACE_SESSION_ALLOWED_EVENT_TYPES
    )

"""§27.9 release blocker: QR pairing through a GFS (pairing ``reach``).

A pairing code can name ONE connection server (GFS) the code owner is
registered with: ``url_gfs`` (inbox URL + GFS fallback) or ``gfs`` (no
address at all). The scanner must be on that same server when the code has
no URL or it has none of its own; the peer-accept / -confirm then ride
the relay sealed to the other side's key-wrap key, and both sides end up
with a MANUAL row opted into the relay, the other's key-wrap key, and one
seeded route — their own connection to the bootstrap server.

Real crypto, real SQLite, the real coordinator, the real relay inbound leg,
the real §24.11 pipeline and transport. Stand-ins: the network (each fake
GFS pushes a ``POST /gfs/envelope`` body down the addressed household's
socket iff it is connected there) and the HTTPS inbox (a household with an
address answers in-process, exactly as ``routes/federation.py`` dispatches
the pairing bootstrap bodies).
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import orjson
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
    FederationEvent,
    FederationEventType,
    GfsConnection,
    GfsNotSharedError,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.federation_capabilities import OURS
from socialhome.federation.federation_service import FederationService
from socialhome.federation.gfs_relay_transport import GfsRelayTransport
from socialhome.federation.pairing_gfs_reach import PairingGfsReach
from socialhome.federation.peer_pairing_client import PeerPairingClient
from socialhome.federation.transport import FederationTransport
from socialhome.global_server.envelope_relay import SEALED_KEYS
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.peer_url import InvalidPeerUrlError
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.services.gfs_relay_inbound import GfsRelayInbound
from socialhome.services.gfs_route_discovery_service import GfsRouteDiscoveryService

pytestmark = pytest.mark.security

URL_G = "https://gfs-g.example.org"
URL_H = "https://gfs-h.example.org"


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
    """A household's relay sender: only its own servers."""

    def __init__(self, net: dict[str, _Gfs], own: set[str]) -> None:
        self._net = net
        self._own = own
        self.sent: list[str] = []

    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        if gfs_url not in self._own:
            return False
        self.sent.append(gfs_url)
        self._net[gfs_url].post(
            {"to_instance": to_instance_id, "sealed": envelope["sealed"]},
        )
        return True


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

        class _Content:
            async def read(self, _n: int) -> bytes:
                return b""

        self.content = _Content()


class _Ctx:
    def __init__(self, coro) -> None:
        self._coro = coro

    async def __aenter__(self):
        return _Resp(await self._coro)

    async def __aexit__(self, *_exc):
        return False


class _Inboxes:
    """In-process HTTPS: a POST to a household's inbox URL runs the pairing
    dispatch ``routes/federation.py`` runs, nothing else."""

    def __init__(self) -> None:
        self.by_base: dict[str, FederationService] = {}
        self.posts: list[str] = []

    def post(self, url, *, data, headers, timeout, allow_redirects):
        self.posts.append(url)
        return _Ctx(self._dispatch(url, data))

    async def _dispatch(self, url: str, data: bytes) -> int:
        base, _, inbox_id = url.rpartition("/")
        federation = self.by_base.get(base)
        if federation is None:
            return 503
        body = orjson.loads(data)
        handler = {
            "pairing_peer_accept": federation.handle_peer_accept,
            "pairing_peer_confirm": federation.handle_peer_confirm,
        }[body["event_type"]]
        try:
            await handler(body, expected_local_inbox_id=inbox_id)
        except ValueError:
            return 403
        return 200


class _DeadInbox:
    async def send(self, *, instance, envelope_dict):
        return False, None


class _NoBootstrap:
    async def handle_bootstrap_body(self, body, *, gfs_url=""):
        raise ValueError("no invite bootstrap in this test")


async def _no_signal(*_a, **_kw):
    raise RuntimeError("no RTC signalling here")


async def _household(tmp_path, name, net, urls, tasks, inboxes, *, base=None):
    db = AsyncDatabase(tmp_path / f"{name}.db", batch_timeout_ms=10)
    await db.startup()
    ident = generate_identity_keypair()
    iid = derive_instance_id(ident.public_key)
    keywrap = generate_x25519_keypair()
    keywrap_sig = b64url_encode(sign_ed25519(ident.private_key, keywrap.public_key))
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
                # The id every household pinned for this server.
                gfs_instance_id=f"gfs-instance-{url[-15:]}",
                display_name=f"server {url}",
                public_key="ab" * 32,
                inbox_url=url,
                status="active",
                paired_at="2026-01-01T00:00:00+00:00",
            ),
        )
    sender = _Sender(net, set(urls))
    transport = FederationTransport(
        own_instance_id=iid,
        https_inbox=_DeadInbox(),
        gfs_relay=GfsRelayTransport(relay_sender=sender),
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
    probed: list[str] = []

    async def _probe(instance_id: str) -> int:
        probed.append(instance_id)
        return await discovery.probe_peer(instance_id)

    federation.attach_pairing_gfs_reach(
        PairingGfsReach(
            gfs_connection_repo=gfs_repo,
            envelope_relay_supported=_relay_capable,
            relay_sender=sender,
            keywrap_public_key=keywrap.public_key,
            keywrap_sig=keywrap_sig,
            probe_peer=_probe,
        ),
    )

    async def _http():
        return inboxes

    federation._pairing.attach_peer_pairing_client(
        PeerPairingClient(own_identity_seed=ident.private_key, client_factory=_http),
    )
    if base:
        inboxes.by_base[base] = federation
    received: list[FederationEvent] = []

    async def _capture(event: FederationEvent) -> None:
        received.append(event)

    federation._event_registry.register(FederationEventType.PRESENCE_UPDATED, _capture)
    return SimpleNamespace(
        name=name,
        db=db,
        iid=iid,
        ident=ident,
        keywrap=keywrap,
        fed_repo=fed_repo,
        federation=federation,
        transport=transport,
        sender=sender,
        conn_ids=conn_ids,
        probed=probed,
        base=base,
        received=received,
    )


@pytest.fixture
async def world(tmp_path):
    tasks: set[asyncio.Task] = set()
    net = {u: _Gfs(u, tasks) for u in (URL_G, URL_H)}
    inboxes = _Inboxes()
    made: list = []

    async def household(name, urls, *, base=None):
        h = await _household(tmp_path, name, net, urls, tasks, inboxes, base=base)
        made.append(h)
        return h

    async def drain() -> None:
        while tasks:
            batch = list(tasks)
            tasks.clear()
            await asyncio.gather(*batch, return_exceptions=True)

    yield SimpleNamespace(household=household, net=net, drain=drain, inboxes=inboxes)
    await drain()
    for h in made:
        await h.db.shutdown()


async def _routes(me, peer) -> list[str]:
    return [r.gfs_connection_id for r in await me.fed_repo.list_gfs_routes(peer.iid)]


async def _pair(world, a, b, qr):
    """b scans, a's admin confirms with b's SAS; drains every relay hop."""
    accepted = await b.federation.accept_pairing(qr, b.base)
    await world.drain()
    await a.federation.confirm_pairing(qr["token"], accepted["verification_code"])
    await world.drain()
    return accepted


async def test_gfs_reach_pairs_two_households_without_any_url(world):
    a = await world.household("a", [URL_G])
    b = await world.household("b", [URL_G])

    qr = await a.federation.initiate_pairing(None, reach="gfs")
    assert qr["inbox_url"] == ""
    assert qr["gfs"] == {"url": URL_G, "instance_id": f"gfs-instance-{URL_G[-15:]}"}

    await _pair(world, a, b, qr)

    for me, peer in ((a, b), (b, a)):
        row = await me.fed_repo.get_instance(peer.iid)
        assert row is not None
        assert row.status is PairingStatus.CONFIRMED
        assert row.source is InstanceSource.MANUAL
        assert row.remote_inbox_url == ""
        assert row.gfs_relay is True
        assert row.remote_keywrap_pk == peer.keywrap.public_key.hex()
        assert row.proto_version == OURS
        # The seeded route is OUR OWN connection to the bootstrap server.
        assert me.conn_ids[URL_G] in await _routes(me, peer)
        assert me.probed == [peer.iid]
    # No inbox was ever contacted.
    assert world.inboxes.posts == []
    # The GFS saw identity-free relay bodies only — no token, no key, no URL.
    assert world.net[URL_G].bodies
    for body in world.net[URL_G].bodies:
        assert set(body) == {"to_instance", "sealed"}
        assert set(body["sealed"]) == set(SEALED_KEYS)
        blob = json.dumps(body)
        assert qr["token"] not in blob
        assert URL_G not in blob

    # Ordinary traffic now rides the seeded route.
    result = await a.federation.send_event(
        to_instance_id=b.iid,
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={"username": "anna", "state": "home"},
    )
    await world.drain()
    assert result.ok is True and result.via == "gfs_relay"
    assert [e.payload["username"] for e in b.received] == ["anna"]


async def test_url_gfs_reach_with_a_scanner_that_has_no_url(world):
    a = await world.household("a", [URL_G], base="https://a.example/federation/inbox")
    b = await world.household("b", [URL_G])

    qr = await a.federation.initiate_pairing(a.base, reach="url_gfs")
    assert qr["reach"] == "url_gfs"
    assert qr["inbox_url"].startswith(a.base + "/")

    await _pair(world, a, b, qr)

    # The accept went to a's inbox (it has one); the confirm had to ride
    # the relay (b has no address).
    assert world.inboxes.posts == [qr["inbox_url"]]
    a_row = await a.fed_repo.get_instance(b.iid)
    b_row = await b.fed_repo.get_instance(a.iid)
    assert a_row.status is PairingStatus.CONFIRMED
    assert b_row.status is PairingStatus.CONFIRMED
    assert a_row.remote_inbox_url == ""
    assert b_row.remote_inbox_url == qr["inbox_url"]
    assert a_row.gfs_relay is True and b_row.gfs_relay is True
    assert await _routes(a, b) == [a.conn_ids[URL_G]]
    assert await _routes(b, a) == [b.conn_ids[URL_G]]


async def test_a_scanner_not_on_the_bootstrap_gfs_is_refused_and_sends_nothing(world):
    a = await world.household("a", [URL_G])
    c = await world.household("c", [URL_H])

    qr = await a.federation.initiate_pairing(None, reach="gfs")
    with pytest.raises(GfsNotSharedError):
        await c.federation.accept_pairing(qr, None)
    await world.drain()

    assert c.sender.sent == []
    assert world.net[URL_G].bodies == [] and world.net[URL_H].bodies == []
    assert await c.fed_repo.get_instance(a.iid) is None
    assert await c.fed_repo.get_pairing(qr["token"]) is None


async def test_a_relayed_accept_through_another_gfs_is_dropped(world):
    """The code named G. A server re-posting the sealed accept onto H (one
    the code owner also uses) gets it dropped: the pairing session accepts
    a relayed body only through the connection the code was issued on."""
    a = await world.household("a", [URL_G, URL_H])
    b = await world.household("b", [URL_G, URL_H])
    qr = await a.federation.initiate_pairing(
        None, reach="gfs", gfs_id=a.conn_ids[URL_G]
    )
    assert qr["gfs"]["url"] == URL_G

    def _misdeliver(body: dict) -> None:
        world.net[URL_G].bodies.append(body)
        world.net[URL_H].post(body)

    world.net[URL_G].post = _misdeliver  # type: ignore[method-assign]
    await b.federation.accept_pairing(qr, None)
    await world.drain()

    assert world.net[URL_H].bodies  # it did arrive — through H
    assert await a.fed_repo.get_instance(b.iid) is None
    session = await a.fed_repo.get_pairing(qr["token"])
    assert session is not None and session.status is PairingStatus.PENDING_SENT


async def test_a_replayed_relayed_accept_after_confirm_is_refused(world):
    a = await world.household("a", [URL_G])
    b = await world.household("b", [URL_G])
    qr = await a.federation.initiate_pairing(None, reach="gfs")
    await _pair(world, a, b, qr)
    accept_blob = next(x for x in world.net[URL_G].bodies if x["to_instance"] == a.iid)
    row_before = await a.fed_repo.get_instance(b.iid)

    inbound = world.net[URL_G].sockets[a.iid]
    with pytest.raises(ValueError, match="No pending pairing"):
        await inbound.handle_frame({"sealed": accept_blob["sealed"]}, gfs_url=URL_G)
    assert await a.fed_repo.get_instance(b.iid) == row_before


async def test_an_expired_session_refuses_the_relayed_accept(world):
    a = await world.household("a", [URL_G])
    b = await world.household("b", [URL_G])
    qr = await a.federation.initiate_pairing(None, reach="gfs")
    await a.db.enqueue(
        "UPDATE pending_pairings SET expires_at=? WHERE token=?",
        ("2020-01-01T00:00:00+00:00", qr["token"]),
    )
    world.net[URL_G].sockets.pop(a.iid)  # hold the blob, deliver it by hand
    await b.federation.accept_pairing(qr, None)
    blob = world.net[URL_G].bodies[-1]

    inbound_a = GfsRelayInbound(
        federation=a.federation,
        keywrap_private_key=a.keywrap.private_key,
        invite_coordinator=_NoBootstrap(),
        gfs_connection_repo=SqliteGfsConnectionRepo(a.db),
    )
    with pytest.raises(ValueError, match="has expired"):
        await inbound_a.handle_frame({"sealed": blob["sealed"]}, gfs_url=URL_G)
    assert await a.fed_repo.get_instance(b.iid) is None


async def test_an_old_scanner_pairs_a_url_gfs_code_url_only(world):
    """A scanner from before reach ignores the extra fields: it answers at
    the URL with no key-wrap key, and the code owner pairs URL-only."""
    a = await world.household("a", [URL_G], base="https://a.example/federation/inbox")
    b = await world.household("b", [], base="https://b.example/federation/inbox")
    qr = await a.federation.initiate_pairing(a.base, reach="url_gfs")
    old_qr = {
        k: v
        for k, v in qr.items()
        if k not in {"reach", "gfs", "keywrap_pk", "keywrap_sig", "keywrap_suite"}
        and k != "proto_version"
    }

    await _pair(world, a, b, old_qr)

    a_row = await a.fed_repo.get_instance(b.iid)
    b_row = await b.fed_repo.get_instance(a.iid)
    assert a_row.status is PairingStatus.CONFIRMED
    assert b_row.status is PairingStatus.CONFIRMED
    assert a_row.remote_inbox_url.startswith(b.base + "/")
    assert a_row.gfs_relay is False and a_row.remote_keywrap_pk is None
    assert b_row.gfs_relay is False
    assert await _routes(a, b) == [] and a.probed == []
    assert world.net[URL_G].bodies == []


async def test_an_old_scanner_fails_closed_on_a_gfs_code(world):
    """Without ``reach`` a ``gfs`` code is a classic code with an empty
    inbox URL — which the old (and current) address check refuses before
    any state exists."""
    a = await world.household("a", [URL_G])
    b = await world.household("b", [], base="https://b.example/federation/inbox")
    qr = await a.federation.initiate_pairing(None, reach="gfs")
    old_qr = {k: v for k, v in qr.items() if k != "reach"}

    with pytest.raises(InvalidPeerUrlError):
        await b.federation.accept_pairing(old_qr, b.base)
    assert await b.fed_repo.get_instance(a.iid) is None
    assert world.inboxes.posts == []

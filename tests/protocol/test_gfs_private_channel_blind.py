"""§27.9 release blocker (v_51): a PRIVATE space's opaque channel tells the
connection server nothing about the space.

Drives a real GFS app end to end with the households' real services: the
owner's :class:`GfsChannelService` registers the channel and announces its
epoch; a link-joined member takes a seat, and a paired member (publish-only,
never a seat) publishes through the real :class:`GfsMemberPublishService`
(trusted, then strict).
Every place the server could see or keep anything is checked — every request
it receives over every session (URL and body), its log records, its database
file and the fan-out frames. None may carry the private space's id, its name
or its authority public key (hex or base64url), nor the space-scoped writer
cert; the channel id is all that names the conversation, and it never rides
in a URL path.
"""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
)
from socialhome.domain.federation import GfsConnection, InstanceSource
from socialhome.domain.gfs_channel import CHANNEL_FRAME_KEYS, CHANNEL_ROUTES
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.writer_cert import WriterEntitlement
from socialhome.global_server.app_keys import (
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.gfs_channel_service import GfsChannelService
from socialhome.services.gfs_member_publish_service import (
    GfsMemberPublishService,
    parse_item_plaintext,
)
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

pytestmark = pytest.mark.security

GFS_ID = "gfs-blind"
SPACE_ID = "sp-private-blind-9f2"
SPACE_NAME = "Grandma's recipes"
EPOCH = 4
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)
AUTHOR = "alice-user-id"


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)


class _Session:
    """Records every request a household session makes."""

    def __init__(self, session, log: list) -> None:
        self._session = session
        self._log = log

    def post(self, url, *, json=None, **kw):
        self._log.append(("POST", str(url), json))
        return self._session.post(url, json=json, **kw)

    def get(self, url, *a, **kw):
        self._log.append(("GET", str(url), None))
        return self._session.get(url, *a, **kw)


class _Gfs:
    def __init__(self, session) -> None:
        self._s = session

    def client(self):
        return self._s

    def publish_client(self):
        return self._s

    async def private_channels_supported(self, conn):
        return True

    async def member_publish_trusted_supported(self, conn):
        return True

    async def member_publish_strict_supported(self, conn):
        return True


class _Spaces:
    def __init__(self, space: Space, *, seed: bytes | None) -> None:
        self.space = space
        self.seed = seed
        self.channel: tuple[str, str] | None = None
        self.instances: list[str] = []

    async def get(self, space_id):
        return self.space if space_id == SPACE_ID else None

    async def get_space_seed(self, space_id):
        return self.seed

    async def list_all(self):
        return [self.space]

    async def list_member_instances(self, space_id):
        return list(self.instances)

    async def get_gfs_channel(self, space_id):
        return self.channel

    async def set_gfs_channel(self, space_id, channel_id, channel_pk):
        self.channel = (channel_id, channel_pk) if channel_id else None
        return True

    async def space_for_gfs_channel(self, channel_id):
        return SPACE_ID if self.channel and self.channel[0] == channel_id else None


class _Keys:
    def __init__(self) -> None:
        self.grants: dict = {}

    async def get_latest(self, space_id):
        return SimpleNamespace(epoch=EPOCH)

    async def set_gfs_channel(self, space_id, epoch, wrapped):
        self.grants[epoch] = wrapped
        return True

    async def get_gfs_channel(self, space_id, epoch):
        return self.grants.get(epoch)


class _Crypto:
    key = AESGCM.generate_key(bit_length=256)

    async def get_current_epoch(self, space_id):
        return EPOCH

    async def encrypt(self, space_id, plaintext: bytes):
        nonce = os.urandom(12)
        return EPOCH, b64url_encode(
            nonce + AESGCM(self.key).encrypt(nonce, plaintext, None)
        )


class _Certs:
    def __init__(self, me: _Household, peers: dict[str, bytes]) -> None:
        self.me = me
        self.peers = peers

    async def own_cert(self, space_id, epoch):
        cert = sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=space_id,
            epoch=epoch,
            instance_pk=self.me.pk,
            scope="write",
        )
        return bind_writer_users(cert, space_seed=SPACE_SEED, user_ids=[AUTHOR])

    async def current_own_cert_wire(self, space_id):
        return (await self.own_cert(space_id, EPOCH)).to_wire()

    async def own_writer_key(self, space_id, epoch):
        return None

    async def verified_instance_pk(self, instance_id, *, claimed=None):
        return self.peers.get(instance_id)

    async def entitlement_for_instance(self, space, instance_id):
        return WriterEntitlement("write", frozenset({AUTHOR}))


class _Remote:
    def __init__(self, seats: set[str]) -> None:
        self.seats = seats

    async def list_for_instance(self, space_id, instance_id, *, include_tombstoned):
        return [object()] if instance_id in self.seats else []


class _FedRepo:
    def __init__(self, link: set[str]) -> None:
        self.link = link

    async def get_instance(self, instance_id):
        source = (
            InstanceSource.SPACE_SESSION
            if instance_id in self.link
            else InstanceSource.MANUAL
        )
        return SimpleNamespace(source=source)


class _Federation:
    async def peer_supports(self, instance_id, *, min_version):
        return True

    async def space_member_supports(self, instance_id, *, min_version):
        return True


class _Conns:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def list_active(self):
        return [self.conn]

    async def get(self, gfs_id):
        return self.conn

    async def list_gfs_for_space(self, space_id):
        return []


def _space(owner: str, *, strict: bool = False) -> Space:
    return Space(
        id=SPACE_ID,
        name=SPACE_NAME,
        owner_instance_id=owner,
        owner_username="o",
        identity_public_key=SPACE_PK.hex(),
        config_sequence=0,
        features=SpaceFeatures(gfs_publish_mode="strict" if strict else "trusted"),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


def _node(h, *, seed, session, conn, peers, seats, link, owner_id):
    spaces = _Spaces(_space(owner_id), seed=seed)
    keys = _Keys()
    certs = _Certs(h, peers)
    gfs = _Gfs(session)
    channels = GfsChannelService(
        gfs=gfs,  # type: ignore[arg-type]
        conn_repo=_Conns(conn),  # type: ignore[arg-type]
        space_repo=spaces,  # type: ignore[arg-type]
        space_key_repo=keys,  # type: ignore[arg-type]
        remote_member_repo=_Remote(seats),  # type: ignore[arg-type]
        federation_repo=_FedRepo(link),  # type: ignore[arg-type]
        space_crypto=_Crypto(),  # type: ignore[arg-type]
        writer_certs=certs,  # type: ignore[arg-type]
        own_instance_id=h.instance_id,
        own_identity_seed=h.seed,
        key_manager=KeyManager(os.urandom(32)),
    )
    channels.attach_federation(_Federation())  # type: ignore[arg-type]
    member = GfsMemberPublishService(
        gfs=gfs,  # type: ignore[arg-type]
        conn_repo=_Conns(conn),  # type: ignore[arg-type]
        space_repo=spaces,  # type: ignore[arg-type]
        space_crypto=_Crypto(),  # type: ignore[arg-type]
        writer_certs=certs,  # type: ignore[arg-type]
        own_instance_id=h.instance_id,
        own_identity_seed=h.seed,
    )
    member.attach_channels(channels)
    return SimpleNamespace(h=h, spaces=spaces, channels=channels, member=member)


@pytest.fixture
async def world(tmp_dir, caplog):
    caplog.set_level(logging.DEBUG, logger="socialhome")
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id=GFS_ID,
        cluster_enabled=False,
        cluster_node_id=GFS_ID,
        cluster_peers=(),
    )
    app = create_gfs_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        owner_h, a_h, b_h = _Household(), _Household(), _Household()
        fed = app[gfs_fed_repo_key]
        for h in (owner_h, a_h, b_h):
            await fed.upsert_instance(
                ClientInstance(
                    instance_id=h.instance_id,
                    display_name="h",
                    public_key=h.pk.hex(),
                    inbox_url="http://h.home/wh",
                    status="active",
                )
            )
            await fed.mark_relay_seen(h.instance_id, at=2**31)
        conn = GfsConnection(
            id="conn-1",
            gfs_instance_id=GFS_ID,
            display_name="GFS",
            public_key="00" * 32,
            inbox_url=str(tc.make_url("")).rstrip("/"),
            status="active",
            paired_at="",
        )
        requests: list = []
        session = _Session(tc.session, requests)
        peers = {a_h.instance_id: a_h.pk, b_h.instance_id: b_h.pk}
        seats = {a_h.instance_id, b_h.instance_id}
        common = dict(
            session=session,
            conn=conn,
            peers=peers,
            seats=seats,
            link={a_h.instance_id},
            owner_id=owner_h.instance_id,
        )
        owner = _node(owner_h, seed=SPACE_SEED, **common)
        owner.spaces.instances = [a_h.instance_id, b_h.instance_id]
        a = _node(a_h, seed=None, **common)
        b = _node(b_h, seed=None, **common)
        for node in (owner, a, b):
            await node.channels.start()
            await node.member.start()
        yield SimpleNamespace(
            app=app,
            owner=owner,
            a=a,
            b=b,
            requests=requests,
            caplog=caplog,
            db_path=tmp_dir / "gfs.db",
            tmp=tmp_dir,
        )
        for node in (owner, a, b):
            await node.member.stop()
            await node.channels.stop()


async def _grant(world, node) -> None:
    raw = await world.owner.channels.grant_for_peer(SPACE_ID, node.h.instance_id)
    assert raw is not None
    assert await node.channels.accept_grant(SPACE_ID, raw)
    await node.channels.wait_idle()


def _leaks() -> tuple[str, ...]:
    return (SPACE_ID, SPACE_NAME, SPACE_PK.hex(), b64url_encode(SPACE_PK))


async def test_the_gfs_never_learns_the_private_space(world):
    assert await world.owner.channels.reconcile(SPACE_ID) == "created"
    await world.owner.channels.announce_epoch(SPACE_ID)
    channel_id = world.owner.spaces.channel[0]
    await _grant(world, world.a)
    await _grant(world, world.b)

    # Trusted: a publishes a post; b's seat gets it.
    # Trusted: paired b (publish-only, no seat) publishes; link-joined a's
    # seat gets it.
    targets = await world.b.member.plan_item(SPACE_ID, AUTHOR, "post")
    assert targets
    inner = {"post_id": "p-1", "space_id": SPACE_ID, "content": "secret recipe"}
    assert await world.b.member.publish_item(SPACE_ID, "post", inner, targets)

    # Strict: the owner switches; new grants carry the channel writer key.
    for node in (world.owner, world.a, world.b):
        node.spaces.space = _space(world.owner.h.instance_id, strict=True)
    await world.owner.channels.announce_epoch(SPACE_ID)
    await _grant(world, world.a)
    await _grant(world, world.b)
    targets = await world.b.member.plan_item(SPACE_ID, AUTHOR, "comment")
    assert targets
    inner2 = {"post_id": "p-1", "space_id": SPACE_ID, "content": "anon comment"}
    assert await world.b.member.publish_item(SPACE_ID, "comment", inner2, targets)

    await world.app[gfs_member_publish_key].wait_idle()
    queued = await world.app[gfs_envelope_queue_repo_key].list_for(
        world.a.h.instance_id, now=0
    )
    assert len(queued) == 2
    # Only the link-joined household holds a seat: the server learns the
    # paired member b only as a (trusted) publisher, never as a subscriber.
    seats = [b for _m, u, b in world.requests if u.endswith("/gfs/channels/subscribe")]
    assert [b["instance_id"] for b in seats] == [world.a.h.instance_id] * len(seats)
    assert seats
    for row in queued:
        assert set(row.sealed) == CHANNEL_FRAME_KEYS
        assert row.sealed["channel_id"] == channel_id
        # The ciphertext really holds the item (and its space-scoped cert).
        raw = b64url_decode(row.sealed["payload"])
        item_type, plain = parse_item_plaintext(
            AESGCM(_Crypto.key).decrypt(raw[:12], raw[12:], None)
        )
        assert plain["writer_cert"]["space_id"] == SPACE_ID

    # 1. Requests: no leak anywhere, no channel id in any URL path, and
    #    every request hit a channel route.
    assert world.requests
    for method, url, body in world.requests:
        text = url + json.dumps(body, sort_keys=True)
        for leak in _leaks():
            assert leak not in text, (method, url)
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        assert path in CHANNEL_ROUTES, path
        assert channel_id not in path
    # The writer cert names the space: it never left the ciphertext.
    assert not any("writer_cert" in json.dumps(b or {}) for _m, _u, b in world.requests)

    # 2. Logs (household and server, at DEBUG): the space is never named
    #    by anything the SERVER logged.
    server_lines = [
        r.getMessage()
        for r in world.caplog.records
        if r.name.startswith("socialhome.global_server")
    ]
    assert server_lines
    for line in server_lines:
        for leak in _leaks():
            assert leak not in line, line

    # 3. Stored rows: the whole database (and WAL) never holds the space.
    dump = b"".join(p.read_bytes() for p in world.tmp.glob("gfs.db*"))
    for leak in _leaks():
        assert leak.encode() not in dump
    assert channel_id.encode() in dump


async def test_a_v50_member_household_gets_no_grant_and_falls_back(world):
    """A member household below v_51 is sent no grant — its items take the
    host path, and nothing about it reaches the channel."""

    class _Old:
        async def peer_supports(self, instance_id, *, min_version):
            return min_version <= 50

        async def space_member_supports(self, instance_id, *, min_version):
            return min_version <= 50

    assert await world.owner.channels.reconcile(SPACE_ID) == "created"
    world.owner.channels.attach_federation(_Old())  # type: ignore[arg-type]
    assert (
        await world.owner.channels.grant_for_peer(SPACE_ID, world.a.h.instance_id)
        is None
    )
    assert await world.a.member.plan_item(SPACE_ID, AUTHOR, "post") == []


async def test_without_the_capability_no_channel_is_created(world):
    class _NoCap(_Gfs):
        async def private_channels_supported(self, conn):
            return False

    world.owner.channels._gfs = _NoCap(None)
    assert await world.owner.channels.reconcile(SPACE_ID) == "none"
    assert world.owner.spaces.channel is None
    assert world.requests == []

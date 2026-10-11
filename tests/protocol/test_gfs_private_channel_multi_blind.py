"""§27.9 release blocker (v_57): a PRIVATE space's channels live only on the
connection servers its OWNER chose, and a member reaches every one of them —
anonymously at the servers it never connected to.

Three real GFS apps: A and B are the owner's chosen channel servers, C is a
connection of the owner it did NOT choose. Member household ``a`` is
connected to A only, member ``b`` to B only (both v_57, trusted mode). The
owner registers the channel on A and B; ``a`` publishes an item; ``b``'s
seat on B receives it — with the host out of the loop.

Checked: C never receives a single request; B never receives anything that
names ``a`` (no instance id, no household signature, no channel cert, no
cookie session — only ``publish-anon`` over the anonymous session); no
server ever sees the space id, its name or its authority key.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import b64url_encode, derive_instance_id, ed25519_public_key
from socialhome.domain.federation import GfsConnection, InstanceSource
from socialhome.domain.gfs_server import GfsServer
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.writer_cert import WriterEntitlement
from socialhome.gfs_server_list_sig import sign_gfs_server_list
from socialhome.global_server.app_keys import (
    gfs_channel_repo_key,
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.gfs_channel_service import GfsChannelService
from socialhome.services.gfs_member_publish_service import GfsMemberPublishService
from socialhome.services.gfs_server_targets import resolve_server_targets
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

pytestmark = pytest.mark.security

SPACE_ID = "sp-private-multi-4d1"
SPACE_NAME = "Cousins only"
EPOCH = 5
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)
AUTHOR = "ada-user-id"
OWNER_SEED = os.urandom(32)
OWNER_PK = ed25519_public_key(OWNER_SEED)
OWNER_ID = derive_instance_id(OWNER_PK)


class _Household:
    def __init__(self, seed: bytes | None = None) -> None:
        self.seed = seed or os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)


class _Session:
    """Records every request a session makes (method, url, body, kind)."""

    def __init__(self, session, log: list, kind: str) -> None:
        self._session = session
        self._log = log
        self._kind = kind

    def post(self, url, *, json=None, **kw):
        self._log.append(("POST", str(url), json, self._kind))
        return self._session.post(url, json=json, **kw)

    def get(self, url, *a, **kw):
        self._log.append(("GET", str(url), None, self._kind))
        return self._session.get(url, *a, **kw)


class _Gfs:
    """The connection-service surface the channel service uses. The owner's
    signed list resolves to real send targets (own connection or listing
    target); the identified session and the anonymous one are told apart."""

    def __init__(self, session, log: list, conns: list[GfsConnection], spaces):
        self._identified = _Session(session, log, "identified")
        self._anon = _Session(session, log, "anonymous")
        self._conns = conns
        self._spaces = spaces

    def client(self):
        return self._identified

    def publish_client(self):
        return self._anon

    @asynccontextmanager
    async def anon_session(self, conn):
        yield self._anon

    async def private_channels_supported(self, conn):
        return True

    async def member_publish_trusted_supported(self, conn):
        return True

    async def member_publish_strict_supported(self, conn):
        return True

    def capability_unknown(self, conn):
        return False

    def known_instance_ids(self, conn):
        return frozenset({conn.gfs_instance_id})

    def addressee_key_for(self, conn):
        return None

    def forget_addressee_key(self, conn):
        pass

    async def refresh_if_stale(self, conn):
        return conn

    async def server_targets(self, space_id):
        space = self._spaces.space
        if space.gfs_servers is None:
            return None
        # The test GFSs live on 127.0.0.1, which a member drops from an
        # owner's list unless it holds that connection (and the pinned
        # session refuses): treat loopback as public here, like the demo
        # harness and ``tests/test_outbound_fetch.py`` do.
        everyone = {
            (e["gfs_instance_id"], e["inbox_url"])
            for e in json.loads(space.gfs_servers.servers_json)
        }
        servers, _ = space.gfs_servers.servers(own_routes=everyone)
        return resolve_server_targets(
            servers, self._conns, known_ids=self.known_instance_ids
        )


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
    async def get_instance(self, instance_id):
        return SimpleNamespace(source=InstanceSource.MANUAL)


class _Federation:
    """Every household is v_57."""

    async def peer_supports(self, instance_id, *, min_version):
        return min_version <= 57

    async def space_member_supports(self, instance_id, *, min_version):
        return min_version <= 57


class _Conns:
    def __init__(self, conns: list[GfsConnection]) -> None:
        self.conns = conns
        #: Pending channel withdrawals (migration 0095).
        self.withdrawals: list[tuple[str, str, str]] = []

    async def list_channel_withdrawals(self, *, space_id=None, gfs_id=None):
        return [
            w
            for w in self.withdrawals
            if (space_id is None or w[0] == space_id)
            and (gfs_id is None or w[1] == gfs_id)
        ]

    async def delete_channel_withdrawal(self, space_id, gfs_id, channel_id):
        self.withdrawals.remove((space_id, gfs_id, channel_id))

    async def list_active(self):
        return list(self.conns)

    async def get(self, gfs_id):
        return next((c for c in self.conns if c.id == gfs_id), None)

    async def list_gfs_for_space(self, space_id):
        return []


def _server(conn: GfsConnection) -> GfsServer:
    return GfsServer(
        gfs_instance_id=conn.gfs_instance_id,
        inbox_url=conn.inbox_url,
        public_key=conn.public_key,
        gfs_key_suite="ed25519",
        display_name=conn.display_name,
    )


def _space(chosen: list[GfsConnection]) -> Space:
    signed = sign_gfs_server_list(
        space_id=SPACE_ID,
        servers=tuple(_server(c) for c in chosen),
        issued_at=1,
        owner_seed=OWNER_SEED,
    )
    return Space(
        id=SPACE_ID,
        name=SPACE_NAME,
        owner_instance_id=OWNER_ID,
        owner_username="o",
        identity_public_key=SPACE_PK.hex(),
        config_sequence=0,
        features=SpaceFeatures(gfs_publish_mode="trusted", private_gfs=True),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        gfs_servers=signed,
    )


def _node(h, *, seed, session, log, conns, space, peers, seats):
    spaces = _Spaces(space, seed=seed)
    keys = _Keys()
    certs = _Certs(h, peers)
    gfs = _Gfs(session, log, conns, spaces)
    channels = GfsChannelService(
        gfs=gfs,  # type: ignore[arg-type]
        conn_repo=_Conns(conns),  # type: ignore[arg-type]
        space_repo=spaces,  # type: ignore[arg-type]
        space_key_repo=keys,  # type: ignore[arg-type]
        remote_member_repo=_Remote(seats),  # type: ignore[arg-type]
        federation_repo=_FedRepo(),  # type: ignore[arg-type]
        space_crypto=_Crypto(),  # type: ignore[arg-type]
        writer_certs=certs,  # type: ignore[arg-type]
        own_instance_id=h.instance_id,
        own_identity_seed=h.seed,
        key_manager=KeyManager(os.urandom(32)),
    )
    channels.attach_federation(_Federation())  # type: ignore[arg-type]
    member = GfsMemberPublishService(
        gfs=gfs,  # type: ignore[arg-type]
        conn_repo=_Conns(conns),  # type: ignore[arg-type]
        space_repo=spaces,  # type: ignore[arg-type]
        space_crypto=_Crypto(),  # type: ignore[arg-type]
        writer_certs=certs,  # type: ignore[arg-type]
        own_instance_id=h.instance_id,
        own_identity_seed=h.seed,
    )
    member.attach_channels(channels)
    return SimpleNamespace(h=h, spaces=spaces, channels=channels, member=member)


def _gfs_app(tmp_dir, name: str):
    data = tmp_dir / name
    data.mkdir()
    return create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url=f"http://{name}.test",
            data_dir=str(data),
            instance_id=f"gfs-{name}",
            cluster_enabled=False,
            cluster_node_id=f"gfs-{name}",
            cluster_peers=(),
        )
    )


async def _register(app, *households: _Household) -> None:
    fed = app[gfs_fed_repo_key]
    for h in households:
        await fed.upsert_instance(
            ClientInstance(
                instance_id=h.instance_id,
                display_name="h",
                public_key=h.pk.hex(),
                status="active",
            )
        )
        await fed.mark_relay_seen(h.instance_id, at=2**31)


def _conn(tc, name: str, key: str) -> GfsConnection:
    return GfsConnection(
        id=f"conn-{name}",
        gfs_instance_id=f"gfs-{name}",
        display_name=name.upper(),
        public_key=key,
        inbox_url=str(tc.make_url("")).rstrip("/"),
        status="active",
        paired_at="",
    )


@pytest.fixture
async def world(tmp_dir):
    apps = {n: _gfs_app(tmp_dir, n) for n in ("a", "b", "c")}
    async with (
        TestClient(TestServer(apps["a"])) as tca,
        TestClient(TestServer(apps["b"])) as tcb,
        TestClient(TestServer(apps["c"])) as tcc,
    ):
        owner_h = _Household(OWNER_SEED)
        a_h, b_h = _Household(), _Household()
        # Who is registered where: the owner on all three; a on A; b on B.
        await _register(apps["a"], owner_h, a_h)
        await _register(apps["b"], owner_h, b_h)
        await _register(apps["c"], owner_h)
        conn_a = _conn(tca, "a", "a1" * 32)
        conn_b = _conn(tcb, "b", "b2" * 32)
        conn_c = _conn(tcc, "c", "c3" * 32)
        space = _space([conn_a, conn_b])  # the owner chose A and B — not C
        log: list = []
        peers = {a_h.instance_id: a_h.pk, b_h.instance_id: b_h.pk}
        seats = {a_h.instance_id, b_h.instance_id}
        common = dict(
            session=tca.session, log=log, space=space, peers=peers, seats=seats
        )
        owner = _node(
            owner_h, seed=SPACE_SEED, conns=[conn_a, conn_b, conn_c], **common
        )
        owner.spaces.instances = [a_h.instance_id, b_h.instance_id]
        a = _node(a_h, seed=None, conns=[conn_a], **common)
        b = _node(b_h, seed=None, conns=[conn_b], **common)
        for node in (owner, a, b):
            await node.channels.start()
            await node.member.start()
        yield SimpleNamespace(
            session=tca.session,
            owner_conns=[conn_a, conn_b, conn_c],
            apps=apps,
            urls={
                "a": conn_a.inbox_url,
                "b": conn_b.inbox_url,
                "c": conn_c.inbox_url,
            },
            owner=owner,
            a=a,
            b=b,
            log=log,
        )
        for node in (owner, a, b):
            await node.member.stop()
            await node.channels.stop()


async def _grant(world, node) -> None:
    raw = await world.owner.channels.grant_for_peer(SPACE_ID, node.h.instance_id)
    assert raw is not None
    assert "writer_key" in raw  # v_57: the channel writer key, trusted mode
    assert await node.channels.accept_grant(SPACE_ID, raw)
    await node.channels.wait_idle()


def _to(world, name: str) -> list:
    return [r for r in world.log if r[1].startswith(world.urls[name] + "/")]


async def _setup(world) -> str:
    assert await world.owner.channels.reconcile(SPACE_ID) == "created"
    assert await world.owner.channels.announce_epoch(SPACE_ID) == 2
    await _grant(world, world.a)
    await _grant(world, world.b)
    return world.owner.spaces.channel[0]


async def test_the_channel_lives_only_on_the_chosen_servers(world):
    channel_id = await _setup(world)
    for name in ("a", "b"):
        assert await world.apps[name][gfs_channel_repo_key].get(channel_id)
    assert await world.apps["c"][gfs_channel_repo_key].get(channel_id) is None
    # The grant names A and B only.
    raw = await world.owner.channels.grant_for_peer(SPACE_ID, world.a.h.instance_id)
    assert sorted(raw["gfs_ids"]) == ["gfs-a", "gfs-b"]
    # C — an active, capable connection the owner did NOT choose — never
    # received a single request about this space or its channel.
    assert _to(world, "c") == []


async def test_a_member_reaches_a_server_it_never_connected_to_anonymously(world):
    await _setup(world)
    world.log.clear()
    # a is connected to A only: its plan covers A AND B (a listing target).
    targets = await world.a.member.plan_item(SPACE_ID, AUTHOR, "post")
    assert sorted(t.gfs_instance_id for t in targets) == ["gfs-a", "gfs-b"]
    assert [t.registered for t in targets if t.gfs_instance_id == "gfs-b"] == [False]
    inner = {"post_id": "p-9", "space_id": SPACE_ID, "content": "hello"}
    accepted = await world.a.member.publish_item(SPACE_ID, "post", inner, targets)
    assert len(accepted) == 2
    # b's seat on B received it — no host involved.
    await world.apps["b"][gfs_member_publish_key].wait_idle()
    queued = await world.apps["b"][gfs_envelope_queue_repo_key].list_for(
        world.b.h.instance_id, now=0
    )
    assert len(queued) == 1
    # Everything a sent to B was anonymous: the anonymous session, the
    # publish-anon route, and nothing that names a.
    to_b = _to(world, "b")
    assert to_b
    for method, url, body, kind in to_b:
        assert kind == "anonymous"
        assert url.endswith("/gfs/channels/publish-anon")
        text = json.dumps(body, sort_keys=True)
        assert world.a.h.instance_id not in text
        assert world.a.h.pk.hex() not in text
        for field_name in ("instance_id", "signature", "channel_cert", "writer_cert"):
            assert field_name not in body
    # B has never heard of household a.
    fed_b = world.apps["b"][gfs_fed_repo_key]
    assert await fed_b.get_instance(world.a.h.instance_id) is None


async def test_no_server_ever_sees_the_space(world):
    await _setup(world)
    targets = await world.a.member.plan_item(SPACE_ID, AUTHOR, "post")
    inner = {"post_id": "p-1", "space_id": SPACE_ID, "content": "secret"}
    await world.a.member.publish_item(SPACE_ID, "post", inner, targets)
    assert world.log
    for _method, url, body, _kind in world.log:
        text = url + json.dumps(body, sort_keys=True)
        for leak in (SPACE_ID, SPACE_NAME, SPACE_PK.hex(), b64url_encode(SPACE_PK)):
            assert leak not in text, url
    assert _to(world, "c") == []


async def test_dropping_a_server_withdraws_the_channel_there(world):
    channel_id = await _setup(world)
    conn_a = next(
        c
        for c in await world.owner.channels._conn_repo.list_active()
        if c.id == "conn-a"
    )
    world.owner.spaces.space = _space([conn_a])
    # The selection service recorded the withdrawal before storing the list.
    world.owner.channels._conn_repo.withdrawals.append((SPACE_ID, "conn-b", channel_id))
    assert await world.owner.channels.on_servers_changed(SPACE_ID) == set()
    assert await world.apps["b"][gfs_channel_repo_key].get(channel_id) is None
    assert await world.apps["a"][gfs_channel_repo_key].get(channel_id)
    raw = await world.owner.channels.grant_for_peer(SPACE_ID, world.a.h.instance_id)
    assert raw["gfs_ids"] == ["gfs-a"]
    assert _to(world, "c") == []


async def test_a_delegated_admins_notice_reaches_b_anonymously(world):
    """A delegated admin (seed holder) connected to A only announces the
    epoch to B too — channel-key signed, over the anonymous session; B
    still never learns the admin's household or the space, and C nothing."""
    await _setup(world)
    world.a.spaces.seed = SPACE_SEED
    world.log.clear()
    assert await world.a.channels.announce_epoch(SPACE_ID) == 2
    to_b = _to(world, "b")
    assert [u.rsplit("/", 1)[-1] for _m, u, _b, _k in to_b] == ["epoch"]
    for _method, _url, body, kind in to_b:
        assert kind == "anonymous"
        text = json.dumps(body, sort_keys=True)
        assert world.a.h.instance_id not in text
        assert SPACE_ID not in text
        assert "instance_id" not in body
    assert _to(world, "c") == []


@pytest.mark.parametrize("stored", ["none", "empty"])
async def test_no_choice_means_no_channel_server_even_after_a_restart(world, stored):
    """The owner turned the option on with several capable connections and
    never picked (no PUT): its list is missing or signed empty. Nothing —
    not the turn-on, not a seat going live, not a rotation, not a restart's
    reconnect heal on each server — contacts ANY GFS for the space."""
    space = world.owner.spaces.space
    signed_empty = sign_gfs_server_list(
        space_id=SPACE_ID, servers=(), issued_at=2, owner_seed=OWNER_SEED
    )
    world.owner.spaces.space = replace(
        space, gfs_servers=None if stored == "none" else signed_empty
    )
    world.log.clear()
    assert await world.owner.channels.enable(SPACE_ID) == "none"
    assert (
        await world.owner.channels.reconcile(SPACE_ID, joining=world.a.h.instance_id)
        == "none"
    )
    assert await world.owner.channels.on_rotation(SPACE_ID) == 0
    # "Restart": a fresh channel service over the same state, every GFS
    # reconnecting.
    owner2 = _node(
        world.owner.h,
        seed=SPACE_SEED,
        session=world.session,
        log=world.log,
        conns=world.owner_conns,
        space=world.owner.spaces.space,
        peers={},
        seats={world.a.h.instance_id, world.b.h.instance_id},
    )
    owner2.spaces.instances = list(world.owner.spaces.instances)
    for conn in world.owner_conns:
        assert await owner2.channels.heal(conn.id) == 0
    await owner2.channels.wait_idle()
    assert world.log == []
    assert world.owner.spaces.channel is None and owner2.spaces.channel is None

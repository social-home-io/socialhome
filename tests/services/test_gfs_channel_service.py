"""Tests for the household side of opaque private-space channels (v_51).

Two (or three) households each run a real :class:`GfsChannelService`
against the REAL connection-server app (real SQLite, real aiohttp), so the
registrations, notices, passes, certs and publishes they sign are checked by
the code that checks them in production. Repositories are in-memory stubs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.domain.events import SpaceRemoteSeatLive
from socialhome.domain.federation import GfsConnection, InstanceSource
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.writer_cert import WriterEntitlement
from socialhome.global_server.app_keys import (
    gfs_channel_repo_key,
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.gfs_channel import (
    channel_pk_of,
    derive_channel_epoch_offset,
    derive_channel_seed,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.gfs_channel_service import GfsChannelService

GFS_ID = "gfs-node-a"
SPACE_ID = "sp-private-7c1"
SPACE_NAME = "The Bakers"


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)


@dataclass
class _Spaces:
    spaces: dict[str, Space] = field(default_factory=dict)
    seeds: dict[str, bytes] = field(default_factory=dict)
    channels: dict[str, tuple[str, str]] = field(default_factory=dict)
    instances: dict[str, list[str]] = field(default_factory=dict)

    async def get(self, space_id):
        return self.spaces.get(space_id)

    async def get_space_seed(self, space_id):
        return self.seeds.get(space_id)

    async def list_all(self):
        return list(self.spaces.values())

    async def list_member_instances(self, space_id):
        return list(self.instances.get(space_id, []))

    async def get_gfs_channel(self, space_id):
        return self.channels.get(space_id)

    async def set_gfs_channel(self, space_id, channel_id, channel_pk):
        if channel_id is None:
            self.channels.pop(space_id, None)
            return True
        if any(c[0] == channel_id and s != space_id for s, c in self.channels.items()):
            return False
        self.channels[space_id] = (channel_id, channel_pk)
        return True

    async def space_for_gfs_channel(self, channel_id):
        return next((s for s, c in self.channels.items() if c[0] == channel_id), None)


class _Keys:
    def __init__(self, epoch: int = 3) -> None:
        self.epoch = epoch
        self.grants: dict[tuple[str, int], str] = {}

    async def get_latest(self, space_id):
        return SimpleNamespace(epoch=self.epoch)

    async def set_gfs_channel(self, space_id, epoch, wrapped):
        if epoch > self.epoch:
            return False
        self.grants[(space_id, epoch)] = wrapped
        return True

    async def get_gfs_channel(self, space_id, epoch):
        return self.grants.get((space_id, epoch))


class _Crypto:
    def __init__(self, keys: _Keys) -> None:
        self.keys = keys

    async def get_current_epoch(self, space_id):
        return self.keys.epoch


class _Remote:
    def __init__(self) -> None:
        self.seats: set[tuple[str, str]] = set()

    async def list_for_instance(self, space_id, instance_id, *, include_tombstoned):
        return [object()] if (space_id, instance_id) in self.seats else []


class _FedRepo:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def get_instance(self, instance_id):
        return self.rows.get(instance_id)


class _Certs:
    def __init__(self) -> None:
        self.pks: dict[str, bytes] = {}
        self.scopes: dict[str, str | None] = {}

    async def verified_instance_pk(self, instance_id, *, claimed=None):
        return self.pks.get(instance_id)

    async def entitlement_for_instance(self, space, instance_id):
        return WriterEntitlement(self.scopes.get(instance_id, "write"))


class _Federation:
    def __init__(self) -> None:
        self.versions: dict[str, int] = {}

    async def peer_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 51) >= min_version


class _Gfs:
    def __init__(self, session, *, capable: bool = True) -> None:
        self.session = session
        self.capable = capable

    async def private_channels_supported(self, conn):
        return self.capable

    def client(self):
        return self.session

    def publish_client(self):
        return self.session


class _Conns:
    def __init__(self, conn: GfsConnection) -> None:
        self.conn = conn

    async def list_active(self):
        return [self.conn]

    async def get(self, gfs_id):
        return self.conn if gfs_id == self.conn.id else None


class _Inbound:
    def __init__(self) -> None:
        self.items: list[tuple[str, int, str]] = []

    async def handle_channel_item(self, space_id, *, epoch, payload):
        self.items.append((space_id, epoch, payload))


class _SpaceService:
    def __init__(self) -> None:
        self.snapshots: list[tuple[str, str]] = []

    async def send_roster_snapshot(self, space_id, *, to_instance_id):
        self.snapshots.append((space_id, to_instance_id))
        return True


def _space(owner: str, *, strict: bool = False, kind=SpaceType.PRIVATE) -> Space:
    return Space(
        id=SPACE_ID,
        name=SPACE_NAME,
        owner_instance_id=owner,
        owner_username="o",
        identity_public_key="",
        config_sequence=0,
        join_mode=JoinMode.INVITE_ONLY,
        space_type=kind,
        features=SpaceFeatures(gfs_publish_mode="strict" if strict else "trusted"),
    )


class _Node:
    """One household: its stubs and its real channel service."""

    def __init__(self, h: _Household, session, conn: GfsConnection) -> None:
        self.h = h
        self.spaces = _Spaces()
        self.keys = _Keys()
        self.remote = _Remote()
        self.fed_repo = _FedRepo()
        self.certs = _Certs()
        self.federation = _Federation()
        self.gfs = _Gfs(session)
        self.inbound = _Inbound()
        self.space_service = _SpaceService()
        self.svc = GfsChannelService(
            gfs=self.gfs,  # type: ignore[arg-type]
            conn_repo=_Conns(conn),  # type: ignore[arg-type]
            space_repo=self.spaces,  # type: ignore[arg-type]
            space_key_repo=self.keys,  # type: ignore[arg-type]
            remote_member_repo=self.remote,  # type: ignore[arg-type]
            federation_repo=self.fed_repo,  # type: ignore[arg-type]
            space_crypto=_Crypto(self.keys),  # type: ignore[arg-type]
            writer_certs=self.certs,  # type: ignore[arg-type]
            own_instance_id=h.instance_id,
            own_identity_seed=h.seed,
            key_manager=KeyManager(os.urandom(32)),
        )
        self.svc.attach_federation(self.federation)  # type: ignore[arg-type]
        self.svc.attach_inbound(self.inbound)  # type: ignore[arg-type]
        self.svc.attach_space_service(self.space_service)


@pytest.fixture
async def env(tmp_dir):
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
        conn = GfsConnection(
            id="conn-1",
            gfs_instance_id=GFS_ID,
            display_name="GFS",
            public_key="",
            inbox_url=str(tc.make_url("")).rstrip("/"),
            status="active",
            paired_at="2026-10-04T00:00:00+00:00",
        )
        session = tc.session
        owner_h, member_h, other_h = _Household(), _Household(), _Household()
        fed = app[gfs_fed_repo_key]
        for h in (owner_h, member_h, other_h):
            await fed.upsert_instance(
                ClientInstance(
                    instance_id=h.instance_id,
                    display_name="H",
                    public_key=h.pk.hex(),
                    inbox_url="http://x",
                    status="active",
                )
            )
            await fed.mark_relay_seen(h.instance_id, at=2**31)
        space_seed = os.urandom(32)
        space = replace(
            _space(owner_h.instance_id),
            identity_public_key=ed25519_public_key(space_seed).hex(),
        )
        owner = _Node(owner_h, session, conn)
        member = _Node(member_h, session, conn)
        other = _Node(other_h, session, conn)
        owner.spaces.spaces[SPACE_ID] = space
        owner.spaces.seeds[SPACE_ID] = space_seed
        owner.spaces.instances[SPACE_ID] = [member_h.instance_id, other_h.instance_id]
        # The member is link-joined (a space_session seat); the other one is
        # a paired member household.
        owner.fed_repo.rows[member_h.instance_id] = SimpleNamespace(
            source=InstanceSource.SPACE_SESSION
        )
        owner.fed_repo.rows[other_h.instance_id] = SimpleNamespace(
            source=InstanceSource.MANUAL
        )
        for h in (member_h, other_h):
            owner.remote.seats.add((SPACE_ID, h.instance_id))
            owner.certs.pks[h.instance_id] = h.pk
        for node in (member, other):
            node.spaces.spaces[SPACE_ID] = space
        yield SimpleNamespace(
            app=app,
            owner=owner,
            member=member,
            other=other,
            space=space,
            space_seed=space_seed,
        )


async def _queued(env, node: _Node) -> list:
    await env.app[gfs_member_publish_key].wait_idle()
    return await env.app[gfs_envelope_queue_repo_key].list_for(
        node.h.instance_id, now=0
    )


def _wire(env, channel_id: str, epoch: int = 3) -> int:
    return epoch + derive_channel_epoch_offset(env.space_seed, SPACE_ID, channel_id)


async def _channel_ready(env) -> str:
    assert await env.owner.svc.reconcile(SPACE_ID) == "created"
    assert await env.owner.svc.announce_epoch(SPACE_ID) == 1
    return env.owner.spaces.channels[SPACE_ID][0]


async def _hand_grants(env, *nodes: _Node) -> None:
    for node in nodes:
        grant = await env.owner.svc.grant_for_peer(SPACE_ID, node.h.instance_id)
        assert grant is not None
        assert await node.svc.accept_grant(SPACE_ID, grant)
        await node.svc.wait_idle()


# ── Which spaces get a channel ───────────────────────────────────────────


async def test_owner_creates_a_channel_the_gfs_cannot_link_to_the_space(env):
    channel_id = await _channel_ready(env)
    repo = env.app[gfs_channel_repo_key]
    row = await repo.get(channel_id)
    # The server holds the CHANNEL epoch (content epoch + secret offset),
    # never the space's content epoch.
    assert row is not None and row.epoch == _wire(env, channel_id) != 3
    expected = derive_channel_seed(env.space_seed, SPACE_ID, channel_id)
    assert row.channel_pk == channel_pk_of(expected)
    # Nothing the server holds names the space, its name or its key.
    blob = str(row)
    for leak in (SPACE_ID, SPACE_NAME, env.space.identity_public_key):
        assert leak not in blob
    assert SPACE_ID not in channel_id


@pytest.mark.parametrize("case", ["public", "no_link_member", "not_owner", "no_gfs"])
async def test_no_channel_unless_eligible(env, case):
    owner = env.owner
    if case == "public":
        owner.spaces.spaces[SPACE_ID] = replace(env.space, space_type=SpaceType.PUBLIC)
    elif case == "no_link_member":
        owner.fed_repo.rows[env.member.h.instance_id] = SimpleNamespace(
            source=InstanceSource.MANUAL
        )
    elif case == "not_owner":
        owner.spaces.spaces[SPACE_ID] = replace(env.space, owner_instance_id="other")
    else:
        owner.gfs.capable = False
    assert await owner.svc.reconcile(SPACE_ID) == "none"
    assert SPACE_ID not in owner.spaces.channels


async def test_a_seat_going_live_creates_and_distributes_the_channel(env):
    # The seat-live event fires before the joiner's space_instances row.
    env.owner.spaces.instances[SPACE_ID] = [env.other.h.instance_id]
    env.owner.space_service.snapshots.clear()
    bus = EventBus()
    env.owner.svc.wire(bus)
    await bus.publish(
        SpaceRemoteSeatLive(
            space_id=SPACE_ID, instance_id=env.member.h.instance_id, user_id="u"
        )
    )
    await env.owner.svc.wait_idle()
    assert SPACE_ID in env.owner.spaces.channels
    snaps = set(env.owner.space_service.snapshots)
    assert snaps == {
        (SPACE_ID, env.other.h.instance_id),
        (SPACE_ID, env.member.h.instance_id),  # the joiner, not yet listed
    }


async def test_the_channel_is_kept_then_retired_with_the_last_link_member(env):
    channel_id = await _channel_ready(env)
    assert await env.owner.svc.reconcile(SPACE_ID) == "kept"
    assert env.owner.spaces.channels[SPACE_ID][0] == channel_id
    # The link-joined member leaves: its seat ends.
    env.owner.remote.seats.discard((SPACE_ID, env.member.h.instance_id))
    assert await env.owner.svc.reconcile(SPACE_ID) == "retired"
    assert SPACE_ID not in env.owner.spaces.channels
    assert await env.app[gfs_channel_repo_key].get(channel_id) is None


async def test_a_new_seed_starts_a_fresh_channel(env):
    first = await _channel_ready(env)
    new_seed = os.urandom(32)
    env.owner.spaces.seeds[SPACE_ID] = new_seed
    env.owner.spaces.spaces[SPACE_ID] = replace(
        env.space, identity_public_key=ed25519_public_key(new_seed).hex()
    )
    assert await env.owner.svc.reconcile(SPACE_ID) == "created"
    assert env.owner.spaces.channels[SPACE_ID][0] != first


async def test_rotation_hook_announces_and_reconciles(env):
    channel_id = await _channel_ready(env)
    env.owner.remote.seats.discard((SPACE_ID, env.member.h.instance_id))
    await env.owner.svc.on_rotation(SPACE_ID)
    assert await env.app[gfs_channel_repo_key].get(channel_id) is None


# ── Grants ───────────────────────────────────────────────────────────────


async def test_no_grant_for_a_v50_peer_or_without_a_seat(env):
    await _channel_ready(env)
    env.owner.federation.versions[env.member.h.instance_id] = 50
    assert (
        await env.owner.svc.grant_for_peer(SPACE_ID, env.member.h.instance_id) is None
    )
    env.owner.federation.versions.pop(env.member.h.instance_id)
    env.owner.remote.seats.discard((SPACE_ID, env.member.h.instance_id))
    assert (
        await env.owner.svc.grant_for_peer(SPACE_ID, env.member.h.instance_id) is None
    )
    # Never to ourselves.
    assert await env.owner.svc.grant_for_peer(SPACE_ID, env.owner.h.instance_id) is None


async def test_a_reader_gets_a_pass_but_no_cert(env):
    await _channel_ready(env)
    env.owner.certs.scopes[env.member.h.instance_id] = None
    grant = await env.owner.svc.grant_for_peer(SPACE_ID, env.member.h.instance_id)
    assert grant is not None
    assert "channel_pass" in grant and "channel_cert" not in grant
    assert "writer_key" not in grant


@pytest.mark.security
async def test_a_member_refuses_a_grant_not_bound_by_the_space_key(env):
    await _channel_ready(env)
    grant = await env.owner.svc.grant_for_peer(SPACE_ID, env.member.h.instance_id)
    assert grant is not None
    forged = {**grant, "gfs_ids": ["evil-gfs"]}
    assert not await env.member.svc.accept_grant(SPACE_ID, forged)
    # Someone else's grant.
    assert not await env.other.svc.accept_grant(SPACE_ID, grant)
    assert SPACE_ID not in env.other.spaces.channels


@pytest.mark.security
async def test_a_grant_naming_another_spaces_channel_is_refused(env):
    channel_id = await _channel_ready(env)
    env.member.spaces.channels["sp-elsewhere"] = (channel_id, "pk")
    grant = await env.owner.svc.grant_for_peer(SPACE_ID, env.member.h.instance_id)
    assert not await env.member.svc.accept_grant(SPACE_ID, grant)


# ── Subscribe + publish ──────────────────────────────────────────────────


async def test_trusted_member_publish_reaches_another_member(env):
    channel_id = await _channel_ready(env)
    await _hand_grants(env, env.member, env.other)
    repo = env.app[gfs_channel_repo_key]
    for node in (env.member, env.other):
        assert await repo.has_subscription(channel_id, node.h.instance_id)
    # The owner holds no grant: it never publishes to (or reads) the channel.
    assert await env.owner.svc.plan(SPACE_ID) == []
    targets = await env.member.svc.plan(SPACE_ID)
    assert [t.id for t in targets] == ["conn-1"]
    accepted = await env.member.svc.publish_sealed(SPACE_ID, 3, "Y2lwaGVy", targets)
    assert len(accepted) == 1
    queued = await _queued(env, env.other)
    assert len(queued) == 1
    frame = queued[0].sealed
    assert frame == {
        "channel_id": channel_id,
        "event_type": "space_item",
        "epoch": _wire(env, channel_id),
        "payload": "Y2lwaGVy",
    }
    # The publisher gets no echo of a trusted publish.
    assert await _queued(env, env.member) == []
    # The receiving household routes it to its space locally.
    await env.other.svc.handle_frame({"type": "relay", **frame})
    assert env.other.inbound.items == [(SPACE_ID, 3, "Y2lwaGVy")]


async def test_strict_member_publish_is_anonymous(env):
    strict = replace(env.space, features=SpaceFeatures(gfs_publish_mode="strict"))
    for node in (env.owner, env.member, env.other):
        node.spaces.spaces[SPACE_ID] = strict
    channel_id = await _channel_ready(env)
    await _hand_grants(env, env.member, env.other)
    row = await env.app[gfs_channel_repo_key].get(channel_id)
    assert row.strict and row.writer_pk_for(_wire(env, channel_id)) is not None
    targets = await env.member.svc.plan(SPACE_ID)
    accepted = await env.member.svc.publish_sealed(SPACE_ID, 3, "YW5vbg", targets)
    assert len(accepted) == 1
    # Anonymous: the server can't exclude the publisher, so both seats get it.
    assert len(await _queued(env, env.other)) == 1
    assert len(await _queued(env, env.member)) == 1


@pytest.mark.security
async def test_strict_space_without_a_writer_key_sends_nothing(env):
    channel_id = await _channel_ready(env)
    await _hand_grants(env, env.member)
    # The owner switches to strict; the member's (trusted) grant has no key.
    strict = replace(env.space, features=SpaceFeatures(gfs_publish_mode="strict"))
    env.member.spaces.spaces[SPACE_ID] = strict
    assert await env.member.svc.plan(SPACE_ID) == []
    conn = (await env.member.svc._capable())[0]
    assert await env.member.svc.publish_sealed(SPACE_ID, 3, "eA", [conn]) == []
    assert await _queued(env, env.other) == []
    assert channel_id


async def test_unknown_channel_frames_are_dropped(env):
    await env.member.svc.handle_frame(
        {
            "type": "relay",
            "channel_id": "a" * 32,
            "event_type": "space_item",
            "epoch": 3,
            "payload": "eA",
        }
    )
    await env.member.svc.handle_frame({"channel_id": "nope"})
    assert env.member.inbound.items == []


async def test_heal_reannounces_and_resubscribes(env):
    channel_id = await _channel_ready(env)
    await _hand_grants(env, env.member)
    repo = env.app[gfs_channel_repo_key]
    await env.member.svc._unsubscribe_all(channel_id, (GFS_ID,))
    assert not await repo.has_subscription(channel_id, env.member.h.instance_id)
    assert await env.member.svc.heal("conn-1") == 1
    assert await repo.has_subscription(channel_id, env.member.h.instance_id)
    assert await env.owner.svc.heal("conn-1") == 1


async def test_a_switch_to_a_new_channel_drops_the_old_seat(env):
    first = await _channel_ready(env)
    await _hand_grants(env, env.member)
    new_seed = os.urandom(32)
    space = replace(env.space, identity_public_key=ed25519_public_key(new_seed).hex())
    env.owner.spaces.seeds[SPACE_ID] = new_seed
    env.owner.spaces.spaces[SPACE_ID] = space
    env.member.spaces.spaces[SPACE_ID] = space
    assert await env.owner.svc.reconcile(SPACE_ID) == "created"
    await env.owner.svc.announce_epoch(SPACE_ID)
    await _hand_grants(env, env.member)
    second = env.member.spaces.channels[SPACE_ID][0]
    assert second != first
    repo = env.app[gfs_channel_repo_key]
    assert await repo.has_subscription(second, env.member.h.instance_id)
    assert not await repo.has_subscription(first, env.member.h.instance_id)


async def test_a_seat_on_an_existing_channel_sends_no_snapshot(env):
    """The joiner's redeem ACK carries its grant; a snapshot right behind
    it would only race the ACK (and land for a space it can't seat yet)."""
    await _channel_ready(env)
    bus = EventBus()
    env.owner.svc.wire(bus)
    await bus.publish(
        SpaceRemoteSeatLive(
            space_id=SPACE_ID, instance_id=env.other.h.instance_id, user_id="u2"
        )
    )
    await env.owner.svc.wait_idle()
    assert env.owner.space_service.snapshots == []


async def test_a_grant_without_a_key_row_does_not_switch_channels(env):
    first = await _channel_ready(env)
    await _hand_grants(env, env.member)
    grant = await env.owner.svc.grant_for_peer(
        SPACE_ID, env.member.h.instance_id, epoch=9
    )
    # A grant for an epoch we hold no key for yet: refused, channel kept.
    fake = {**grant, "channel_id": "f" * 32}
    assert not await env.member.svc.accept_grant(SPACE_ID, fake)
    assert not await env.member.svc.accept_grant(SPACE_ID, grant)
    assert env.member.spaces.channels[SPACE_ID][0] == first


@pytest.mark.security
async def test_the_owner_never_gets_or_takes_a_grant(env):
    """A delegated admin's rekey fans out to the owner too: it must carry no
    grant for it, and the owner refuses one — a seat would name it."""
    await _channel_ready(env)
    # Issued by a seed holder for the owner household: refused at issue.
    assert await env.owner.svc.grant_for_peer(SPACE_ID, env.owner.h.instance_id) is None
    # And should one arrive anyway (forged into a rekey by a delegated
    # admin), the owner refuses to hold it.
    grant = await env.owner.svc.grant_for_peer(SPACE_ID, env.member.h.instance_id)
    assert not await env.owner.svc.accept_grant(SPACE_ID, grant)


async def test_a_late_older_grant_never_moves_back_to_a_replaced_channel(env):
    first = await _channel_ready(env)
    await _hand_grants(env, env.member)
    old = await env.owner.svc.grant_for_peer(SPACE_ID, env.member.h.instance_id)
    # A fresh channel at a newer epoch.
    new_seed = os.urandom(32)
    space = replace(env.space, identity_public_key=ed25519_public_key(new_seed).hex())
    env.owner.spaces.seeds[SPACE_ID] = new_seed
    env.owner.spaces.spaces[SPACE_ID] = space
    env.member.spaces.spaces[SPACE_ID] = space
    env.owner.keys.epoch = env.member.keys.epoch = 4
    assert await env.owner.svc.reconcile(SPACE_ID) == "created"
    await _hand_grants(env, env.member)
    second = env.member.spaces.channels[SPACE_ID][0]
    assert second != first
    # The old grant (epoch 3, old pin) no longer even verifies; a late copy
    # of any older grant must not switch us back.
    assert old is not None
    assert not await env.member.svc.accept_grant(SPACE_ID, old)
    assert env.member.spaces.channels[SPACE_ID][0] == second


async def test_a_squatted_channel_id_is_replaced_by_a_fresh_one(env):
    """A member that learned the id pre-registers it where our registration
    had not landed: we never fight over it — a fresh channel."""
    first = await _channel_ready(env)
    # Simulate the squat on the same server: the id now pins another key.
    repo = env.app[gfs_channel_repo_key]
    await repo.delete(first)
    await repo.register(first, channel_suite="ed25519", channel_pk="squatter", now=1)
    assert await env.owner.svc.reconcile(SPACE_ID) == "created"
    assert env.owner.spaces.channels[SPACE_ID][0] != first

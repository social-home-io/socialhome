"""Tests for the household side of trusted-mode member publish (v_49).

The household service runs against the REAL connection-server app (real
SQLite, real aiohttp) so the signatures it produces — the identified member
publish with ``gfs_instance_id``, the owner's household-signed epoch notice,
a delegated admin's authority-signed one — are checked by the code that will
check them in production.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
)
from socialhome.domain.events import SpaceMemberJoined
from socialhome.domain.federation import GfsConnection
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.writer_cert import WriterCert
from socialhome.global_server.app_keys import (
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
    gfs_space_epoch_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.gfs_member_publish_service import (
    GfsMemberPublishService,
    build_item_plaintext,
    parse_item_plaintext,
)
from socialhome.services.gfs_publish_retry import GfsPublish
from socialhome.writer_cert import bind_writer_users, sign_writer_cert

SPACE_ID = "sp-pub"
AUTHOR = "u-me"
GFS_ID = "gfs-node-a"
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)


class _Crypto:
    """A one-key AES-GCM stand-in for ``SpaceContentEncryption``."""

    def __init__(self, epoch: int = 3) -> None:
        self.epoch = epoch
        self.key = AESGCM.generate_key(bit_length=256)

    async def get_current_epoch(self, space_id):
        return self.epoch

    async def encrypt(self, space_id, plaintext: bytes):
        nonce = os.urandom(12)
        return self.epoch, b64url_encode(
            nonce + AESGCM(self.key).encrypt(nonce, plaintext, None)
        )

    def decrypt(self, ct: str) -> bytes:
        raw = b64url_decode(ct)
        return AESGCM(self.key).decrypt(raw[:12], raw[12:], None)


class _Certs:
    def __init__(self, holder: _Household, *, scope: str = "write") -> None:
        self.holder = holder
        self.scope: str | None = scope
        self.users: list[str] = [AUTHOR]

    async def own_cert(self, space_id, epoch) -> WriterCert | None:
        if self.scope is None:
            return None
        cert = sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=space_id,
            epoch=epoch,
            instance_pk=self.holder.pk,
            scope=self.scope,
        )
        return bind_writer_users(cert, space_seed=SPACE_SEED, user_ids=self.users)

    async def current_own_cert_wire(self, space_id):
        cert = await self.own_cert(space_id, 3)
        return cert.to_wire() if cert is not None else None


@dataclass
class _Spaces:
    spaces: dict[str, Space] = field(default_factory=dict)
    seeds: dict[str, bytes] = field(default_factory=dict)
    local: dict[str, list[str]] = field(default_factory=dict)
    members: dict[str, list[SpaceMember]] = field(default_factory=dict)

    async def get(self, space_id):
        return self.spaces.get(space_id)

    async def get_space_seed(self, space_id):
        return self.seeds.get(space_id)

    async def list_all(self):
        return list(self.spaces.values())

    async def list_local_member_user_ids(self, space_id):
        return self.local.get(space_id, [])

    async def list_members(self, space_id):
        return self.members.get(space_id, [])


class _Conns:
    def __init__(self, conns: list[GfsConnection], own: list[str] | None = None):
        self.conns = conns
        self.own = own or []

    async def list_active(self):
        return [c for c in self.conns if c.status == "active"]

    async def list_gfs_for_space(self, space_id):
        return [c for c in self.conns if c.id in self.own]

    async def get(self, gfs_id):
        return next((c for c in self.conns if c.id == gfs_id), None)


class _Gfs:
    """The slice of ``GfsConnectionService`` the member service uses."""

    def __init__(self, session: aiohttp.ClientSession, *, capable: bool = True):
        self.session = session
        self.capable = capable
        self.subscribed: list[tuple[str, str]] = []

    def client(self):
        return self.session

    def publish_client(self):
        return self.session

    async def member_publish_trusted_supported(self, conn):
        return self.capable

    async def subscribe_to_gfs_space(self, space_id, gfs_id):
        self.subscribed.append((space_id, gfs_id))
        return "subscribed"


def _space(*, owner: str = "host.home", public=True, readable=True, sid=SPACE_ID):
    return Space(
        id=sid,
        name="Public",
        owner_instance_id=owner,
        owner_username="o",
        identity_public_key=SPACE_PK.hex(),
        config_sequence=0,
        features=SpaceFeatures(allow_subscribers=readable),
        space_type=SpaceType.PUBLIC if public else SpaceType.PRIVATE,
        join_mode=JoinMode.OPEN,
    )


@pytest.fixture
async def world(tmp_dir):
    """A real GFS with the space listed, a member household (``me``), the
    space owner household and an offline subscriber seen recently."""
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
        me, owner, sub = _Household(), _Household(), _Household()
        fed = app[gfs_fed_repo_key]
        for h in (me, owner, sub):
            await fed.upsert_instance(
                ClientInstance(
                    instance_id=h.instance_id,
                    display_name="h",
                    public_key=h.pk.hex(),
                    inbox_url="http://h.home/wh",
                    status="active",
                )
            )
        await fed.upsert_space(
            GlobalSpace(
                space_id=SPACE_ID,
                owning_instance=owner.instance_id,
                name="Public",
                allow_subscribers=True,
                status="active",
                identity_public_key=SPACE_PK.hex(),
            )
        )
        await fed.add_subscriber(space_id=SPACE_ID, instance_id=sub.instance_id)
        await fed.mark_relay_seen(sub.instance_id, at=int(time.time()))
        conn = GfsConnection(
            id="conn-1",
            gfs_instance_id=GFS_ID,
            display_name="GFS",
            public_key="00" * 32,
            inbox_url=str(tc.make_url("")).rstrip("/"),
            status="active",
            paired_at="",
        )
        spaces = _Spaces(spaces={SPACE_ID: _space(owner=owner.instance_id)})
        crypto = _Crypto()
        gfs = _Gfs(tc.session)
        svc = GfsMemberPublishService(
            gfs=gfs,  # type: ignore[arg-type]
            conn_repo=_Conns([conn]),  # type: ignore[arg-type]
            space_repo=spaces,  # type: ignore[arg-type]
            space_crypto=crypto,  # type: ignore[arg-type]
            writer_certs=_Certs(me),  # type: ignore[arg-type]
            own_instance_id=me.instance_id,
            own_identity_seed=me.seed,
        )
        await svc.start()
        yield {
            "tc": tc,
            "app": app,
            "svc": svc,
            "me": me,
            "owner": owner,
            "sub": sub,
            "conn": conn,
            "spaces": spaces,
            "crypto": crypto,
            "gfs": gfs,
        }
        await svc.stop()


def _owner_svc(world) -> GfsMemberPublishService:
    """The same household setup, but acting as the space OWNER."""
    owner = world["owner"]
    world["spaces"].seeds[SPACE_ID] = SPACE_SEED
    return GfsMemberPublishService(
        gfs=world["gfs"],
        conn_repo=_Conns([world["conn"]]),  # type: ignore[arg-type]
        space_repo=world["spaces"],  # type: ignore[arg-type]
        space_crypto=world["crypto"],  # type: ignore[arg-type]
        writer_certs=_Certs(owner),  # type: ignore[arg-type]
        own_instance_id=owner.instance_id,
        own_identity_seed=owner.seed,
    )


async def _queued(world) -> list:
    await world["app"][gfs_member_publish_key].wait_idle()
    return await world["app"][gfs_envelope_queue_repo_key].list_for(
        world["sub"].instance_id, now=0
    )


def _inner(**extra) -> dict:
    return {"post_id": "p-1", "space_id": SPACE_ID, "content": "hello", **extra}


# ── Plaintext codec ──────────────────────────────────────────────────────


def test_item_plaintext_round_trips_and_refuses_the_rest():
    pt = build_item_plaintext("post", {"a": 1})
    assert parse_item_plaintext(pt) == ("post", {"a": 1})
    for bad in (
        b"x",
        b"[]",
        build_item_plaintext("poll", {}),
        b'{"item_type":"post"}',
    ):
        assert parse_item_plaintext(bad) is None


# ── Planning ─────────────────────────────────────────────────────────────


async def test_plan_targets_a_capable_server_listing_the_space(world):
    assert [c.id for c in await world["svc"].plan_post(SPACE_ID, AUTHOR)] == ["conn-1"]


@pytest.mark.parametrize(
    "setup",
    ["private", "unreadable", "seed_holder", "no_cert", "comment_cert", "incapable"],
)
async def test_plan_is_empty_when_the_household_must_not_publish(world, setup):
    svc, spaces = world["svc"], world["spaces"]
    if setup == "private":
        spaces.spaces[SPACE_ID] = _space(public=False)
    elif setup == "unreadable":
        spaces.spaces[SPACE_ID] = _space(readable=False)
    elif setup == "seed_holder":
        spaces.seeds[SPACE_ID] = SPACE_SEED
    elif setup == "no_cert":
        svc._writer_certs.scope = None
    elif setup == "comment_cert":
        svc._writer_certs.scope = "comment"
    elif setup == "incapable":
        world["gfs"].capable = False
    assert await svc.plan_post(SPACE_ID, AUTHOR) == []


async def test_a_server_not_listing_the_space_is_not_a_target(world):
    spaces = world["spaces"]
    spaces.spaces["sp-unlisted"] = _space(sid="sp-unlisted")
    assert await world["svc"].plan_post("sp-unlisted", AUTHOR) == []


async def test_a_server_we_published_to_counts_as_listing_without_a_probe(world):
    svc = world["svc"]
    svc._conn_repo.own = ["conn-1"]
    world["spaces"].spaces["sp-unlisted"] = _space(sid="sp-unlisted")
    assert [c.id for c in await svc.plan_post("sp-unlisted", AUTHOR)] == ["conn-1"]


async def test_the_listing_answer_is_cached(world, monkeypatch):
    svc = world["svc"]
    assert await svc._listed(world["conn"], SPACE_ID)
    calls = 0
    real_get = world["tc"].session.get

    def _counting_get(*a, **kw):
        nonlocal calls
        calls += 1
        return real_get(*a, **kw)

    monkeypatch.setattr(world["tc"].session, "get", _counting_get)
    assert await svc._listed(world["conn"], SPACE_ID)
    assert calls == 0


# ── Publishing ───────────────────────────────────────────────────────────


@pytest.mark.security
async def test_a_published_post_is_accepted_and_carries_only_ciphertext(world):
    svc = world["svc"]
    targets = await svc.plan_post(SPACE_ID, AUTHOR)
    inner = _inner(writer_cert={"stale": True})
    assert [c.id for c in await svc.publish_post(SPACE_ID, inner, targets)] == [
        "conn-1"
    ]
    rows = await _queued(world)
    assert len(rows) == 1
    frame = rows[0].sealed
    assert frame["event_type"] == "space_item"
    assert "hello" not in json.dumps(frame)
    item_type, got = parse_item_plaintext(world["crypto"].decrypt(frame["payload"]))
    assert item_type == "post"
    # Our own fresh cert rides inside — with its user binding, which never
    # appears in plaintext (the frame carries the v1 fields only).
    assert (
        WriterCert.from_wire(got["writer_cert"]).v1().to_wire() == frame["writer_cert"]
    )
    assert got["writer_cert"]["writer_user_ids"] == [AUTHOR]
    assert "writer_user_ids" not in frame["writer_cert"]
    assert got["content"] == "hello"


@pytest.mark.security
async def test_the_request_is_signed_for_the_pinned_server_id(world):
    """``gfs_instance_id`` is inside the signature: a request built for a
    connection pinned to another id is refused (403, never retried)."""
    svc = world["svc"]
    wrong = replace(world["conn"], gfs_instance_id="gfs-node-b")
    data = {
        "epoch": 3,
        "writer_cert": (await svc._writer_certs.own_cert(SPACE_ID, 3)).to_wire(),
        "payload": "Y3Q",
    }
    body = svc._signed_item_body(wrong, SPACE_ID, data)
    assert body["gfs_instance_id"] == "gfs-node-b"
    outcome = await svc._post_item(wrong, SPACE_ID, data)
    assert outcome.kind == "permanent"
    outcome = await svc._post_item(world["conn"], SPACE_ID, data)
    assert outcome.kind == "delivered"


async def test_a_transient_failure_is_queued_and_re_signed_on_retry(world):
    svc = world["svc"]
    down = replace(world["conn"], inbox_url="http://127.0.0.1:9")
    svc._conn_repo.conns = [down]
    data = {
        "epoch": 3,
        "writer_cert": (await svc._writer_certs.own_cert(SPACE_ID, 3)).to_wire(),
        "payload": "Y3Q",
    }
    item = GfsPublish(space_id=SPACE_ID, event_type="space_item", payload=data)
    assert not await svc._first_attempt(down, item)
    assert svc._retry.pending(down.id)
    # The queued item holds no timestamp or signature: every attempt signs
    # afresh (a request is only valid for ±300 s).
    queued = svc._retry._queues[down.id].items[0]
    assert "ts" not in queued.payload and "signature" not in queued.payload


async def test_a_retry_drops_the_item_once_the_capability_is_gone(world):
    svc = world["svc"]
    world["gfs"].capable = False
    item = GfsPublish(space_id=SPACE_ID, event_type="space_item", payload={})
    assert (await svc._retry_send("conn-1", item)).kind == "permanent"
    assert (await svc._retry_send("missing", item)).kind == "permanent"


async def test_without_the_capability_nothing_identified_is_sent(world, monkeypatch):
    world["gfs"].capable = False
    posted = []
    monkeypatch.setattr(
        world["tc"].session, "post", lambda *a, **kw: posted.append(a) or None
    )
    svc = world["svc"]
    assert await svc.plan_post(SPACE_ID, AUTHOR) == []
    assert await svc.publish_post(SPACE_ID, _inner(), []) == []
    assert await svc.announce_epoch(SPACE_ID) == 0
    assert posted == []


async def test_no_cert_for_the_sealing_epoch_publishes_nothing(world):
    svc = world["svc"]
    targets = await svc.plan_post(SPACE_ID, AUTHOR)
    svc._writer_certs.scope = None
    assert await svc.publish_post(SPACE_ID, _inner(), targets) == []
    assert await _queued(world) == []


# ── Epoch notices ────────────────────────────────────────────────────────


async def _epoch(world):
    return await world["app"][gfs_space_epoch_repo_key].get(SPACE_ID)


async def test_the_owner_announces_with_its_household_signature(world):
    owner_svc = _owner_svc(world)
    world["crypto"].epoch = 7
    assert await owner_svc.announce_epoch(SPACE_ID) == 1
    state = await _epoch(world)
    assert (state.confirmed, state.current) == (7, 7)
    # An owner may jump (a v_44 restore lifts the epoch to unix seconds).
    world["crypto"].epoch = int(time.time())
    assert await owner_svc.announce_epoch(SPACE_ID) == 1
    assert (await _epoch(world)).confirmed == world["crypto"].epoch


async def test_a_delegated_admin_announces_the_authority_signed_plus_one(world):
    await _owner_svc(world).announce_epoch(SPACE_ID)  # owner confirms 3
    await world["app"][gfs_space_epoch_repo_key].confirm(
        SPACE_ID, 3, now=int(time.time()) - 120
    )
    db_state = await _epoch(world)
    assert db_state.confirmed == 3
    # ``me`` holds the seed as a delegated admin (not the owner).
    world["spaces"].seeds[SPACE_ID] = SPACE_SEED
    world["crypto"].epoch = 4
    await world["app"][gfs_space_epoch_repo_key]._db.enqueue(
        "UPDATE global_spaces SET content_epoch_raised_at=0 WHERE space_id=?",
        (SPACE_ID,),
    )
    assert await world["svc"].announce_epoch(SPACE_ID) == 1
    state = await _epoch(world)
    assert (state.current, state.confirmed) == (4, 3)


async def test_no_seed_or_no_readability_announces_nothing(world):
    svc = world["svc"]
    assert await svc.announce_epoch(SPACE_ID) == 0  # no seed held
    world["spaces"].seeds[SPACE_ID] = SPACE_SEED
    world["spaces"].spaces[SPACE_ID] = _space(readable=False)
    assert await svc.announce_epoch(SPACE_ID) == 0


async def test_announce_held_epochs_covers_seed_held_spaces_on_one_server(world):
    owner_svc = _owner_svc(world)
    world["crypto"].epoch = 9
    assert await owner_svc.announce_held_epochs("conn-1") == 1
    assert (await _epoch(world)).confirmed == 9
    assert await owner_svc.announce_held_epochs("missing") == 0


# ── Auto-subscribe ───────────────────────────────────────────────────────


def _member(user_id: str, role: SpaceRole) -> SpaceMember:
    return SpaceMember(
        space_id=SPACE_ID, user_id=user_id, role=role.value, joined_at="2026-10-03"
    )


async def test_a_writer_household_subscribes_to_its_spaces(world):
    spaces = world["spaces"]
    spaces.local[SPACE_ID] = ["u1"]
    spaces.members[SPACE_ID] = [_member("u1", SpaceRole.MEMBER)]
    assert await world["svc"].subscribe_member_spaces("conn-1") == 1
    assert world["gfs"].subscribed == [(SPACE_ID, "conn-1")]


@pytest.mark.parametrize("case", ["follower_only", "seed_holder", "incapable", "none"])
async def test_auto_subscribe_skips_what_it_must(world, case):
    spaces = world["spaces"]
    spaces.local[SPACE_ID] = ["u1"]
    spaces.members[SPACE_ID] = [_member("u1", SpaceRole.MEMBER)]
    if case == "follower_only":
        spaces.members[SPACE_ID] = [_member("u1", SpaceRole.SUBSCRIBER)]
    elif case == "seed_holder":
        spaces.seeds[SPACE_ID] = SPACE_SEED
    elif case == "incapable":
        world["gfs"].capable = False
    elif case == "none":
        spaces.local[SPACE_ID] = []
    assert await world["svc"].subscribe_member_spaces("conn-1") == 0
    assert world["gfs"].subscribed == []


async def test_a_failing_subscribe_is_fail_soft(world):
    spaces = world["spaces"]
    spaces.local[SPACE_ID] = ["u1"]
    spaces.members[SPACE_ID] = [_member("u1", SpaceRole.ADMIN)]

    async def _boom(space_id, gfs_id):
        raise RuntimeError("down")

    world["gfs"].subscribe_to_gfs_space = _boom
    assert await world["svc"].subscribe_member_spaces("conn-1") == 0


async def test_planning_a_post_subscribes_once(world):
    svc = world["svc"]
    await svc.plan_post(SPACE_ID, AUTHOR)
    await svc.plan_post(SPACE_ID, AUTHOR)
    assert world["gfs"].subscribed == [(SPACE_ID, "conn-1")]


async def test_a_local_writer_seat_subscribes_on_join(world):
    spaces = world["spaces"]
    spaces.local[SPACE_ID] = ["u1"]
    spaces.members[SPACE_ID] = [_member("u1", SpaceRole.MEMBER)]
    bus = EventBus()
    world["svc"].wire(bus)
    await bus.publish(SpaceMemberJoined(space_id=SPACE_ID, user_id="u1"))
    assert world["gfs"].subscribed == [(SPACE_ID, "conn-1")]


async def test_ensure_subscribed_skips_followers_and_seed_holders(world):
    spaces = world["spaces"]
    spaces.local[SPACE_ID] = ["u1"]
    spaces.members[SPACE_ID] = [_member("u1", SpaceRole.SUBSCRIBER)]
    assert await world["svc"].ensure_subscribed(SPACE_ID) == 0
    spaces.members[SPACE_ID] = [_member("u1", SpaceRole.MEMBER)]
    spaces.seeds[SPACE_ID] = SPACE_SEED
    assert await world["svc"].ensure_subscribed(SPACE_ID) == 0
    assert world["gfs"].subscribed == []


async def test_a_failing_join_subscribe_is_logged_not_raised(world, monkeypatch):
    async def _boom(self, space_id):
        raise RuntimeError("down")

    monkeypatch.setattr(GfsMemberPublishService, "ensure_subscribed", _boom)
    bus = EventBus()
    world["svc"].wire(bus)
    await bus.publish(SpaceMemberJoined(space_id=SPACE_ID, user_id="u1"))


# ── v2: author binding, accepted-only targets, directory listing ─────────


async def test_an_author_outside_our_cert_binding_takes_the_host_path(world):
    """A plain member of a moderated / admin-only space (or any user the
    cert does not bind) publishes nothing itself — its post goes through
    the host (queue / refusal)."""
    svc = world["svc"]
    assert await svc.plan_post(SPACE_ID, "u-other") == []


async def test_a_v1_cert_without_a_binding_takes_the_host_path(world, monkeypatch):
    svc = world["svc"]

    async def _v1(space_id):
        cert = sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=space_id,
            epoch=3,
            instance_pk=world["me"].pk,
            scope="write",
        )
        return cert.to_wire()

    monkeypatch.setattr(svc._writer_certs, "current_own_cert_wire", _v1)
    assert await svc.plan_post(SPACE_ID, AUTHOR) == []


async def test_only_servers_that_accepted_are_returned(world):
    svc = world["svc"]
    down = replace(world["conn"], id="conn-down", inbox_url="http://127.0.0.1:9")
    accepted = await svc.publish_post(SPACE_ID, _inner(), [world["conn"], down])
    assert [c.id for c in accepted] == ["conn-1"]
    assert svc._retry.pending("conn-down")


async def test_schedule_post_publishes_in_the_background(world):
    svc = world["svc"]
    assert svc.schedule_post(SPACE_ID, AUTHOR, _inner())
    await svc.wait_idle()
    assert len(await _queued(world)) == 1


async def test_schedule_post_for_an_author_without_rights_publishes_nothing(world):
    svc = world["svc"]
    assert svc.schedule_post(SPACE_ID, "u-other", _inner())
    await svc.wait_idle()
    assert await _queued(world) == []


async def test_schedule_post_refuses_while_stopping_or_saturated(world, monkeypatch):
    svc = world["svc"]
    await svc.stop()
    assert not svc.schedule_post(SPACE_ID, AUTHOR, _inner())
    await svc.start()
    monkeypatch.setattr(
        "socialhome.services.gfs_member_publish_service.MAX_PENDING_PUBLISHES", 0
    )
    assert not svc.schedule_post(SPACE_ID, AUTHOR, _inner())


async def test_a_failing_background_publish_is_logged(world, monkeypatch, caplog):
    svc = world["svc"]

    async def _boom(self, space_id, author_user_id, item_type):
        raise RuntimeError("down")

    monkeypatch.setattr(GfsMemberPublishService, "plan_item", _boom)
    with caplog.at_level("ERROR"):
        svc.schedule_post(SPACE_ID, AUTHOR, _inner())
        await svc.wait_idle()
    assert "background publish failed" in caplog.text


async def test_listing_reads_the_whole_directory_never_a_space_probe(
    world, monkeypatch
):
    svc = world["svc"]
    seen: list[str] = []
    real_get = world["tc"].session.get

    def _spy(url, *a, **kw):
        seen.append(str(url))
        return real_get(url, *a, **kw)

    monkeypatch.setattr(world["tc"].session, "get", _spy)
    assert await svc._listed(world["conn"], SPACE_ID)
    assert not await svc._listed(world["conn"], "sp-unlisted")
    assert seen == [f"{world['conn'].inbox_url}/gfs/spaces"]


async def test_listing_without_a_publish_session_lists_nothing(world, monkeypatch):
    monkeypatch.setattr(world["gfs"], "publish_client", lambda: None)
    assert not await world["svc"]._listed(world["conn"], SPACE_ID)


# ── v_49 PR 3: comments, reactions, own edits / deletes ──────────────────


def test_every_member_item_type_round_trips_the_plaintext_codec():
    for item_type in (
        "post_edit",
        "post_delete",
        "comment",
        "comment_edit",
        "comment_delete",
        "reaction_add",
        "reaction_remove",
    ):
        assert parse_item_plaintext(build_item_plaintext(item_type, {"a": 1})) == (
            item_type,
            {"a": 1},
        )


@pytest.mark.parametrize(
    ("item_type", "scope", "planned"),
    [
        ("comment", "comment", True),
        ("comment", "write", True),
        ("reaction_add", "comment", True),
        ("comment_delete", "comment", True),
        # A post edit or delete needs the post's own right.
        ("post_edit", "comment", False),
        ("post_delete", "comment", False),
        ("post_delete", "write", True),
    ],
)
async def test_plan_item_follows_the_scope_the_type_needs(
    world, item_type, scope, planned
):
    svc = world["svc"]
    svc._writer_certs.scope = scope
    targets = await svc.plan_item(SPACE_ID, AUTHOR, item_type)
    assert bool(targets) is planned


async def test_plan_item_needs_the_author_in_the_binding(world):
    svc = world["svc"]
    svc._writer_certs.users = ["someone-else"]
    assert await svc.plan_item(SPACE_ID, AUTHOR, "comment") == []


async def test_a_seed_holder_publishes_items_but_not_posts(world):
    """The host relays posts with the authority signature, but nothing else
    reaches followers: its comments, reactions and own edits / deletes go
    over the member relay like any member's."""
    svc = _owner_svc(world)
    assert await svc.plan_post(SPACE_ID, AUTHOR) == []
    assert [c.id for c in await svc.plan_item(SPACE_ID, AUTHOR, "comment")] == [
        "conn-1"
    ]
    # A seed holder never auto-subscribes: it receives over federation.
    assert world["gfs"].subscribed == []


@pytest.mark.security
async def test_a_published_comment_carries_only_ciphertext_and_its_real_type(world):
    """(With a ``write`` cert: a ``comment``-scope cert names its scope in
    plaintext, which the GFS is meant to see — never the item type.)"""
    svc = world["svc"]
    targets = await svc.plan_item(SPACE_ID, AUTHOR, "comment")
    accepted = await svc.publish_item(
        SPACE_ID, "comment", {"item_target": "c-1", "content": "hello"}, targets
    )
    assert [c.id for c in accepted] == ["conn-1"]
    frame = (await _queued(world))[0].sealed
    assert frame["event_type"] == "space_item"
    assert "comment" not in json.dumps(frame)
    assert "hello" not in json.dumps(frame)
    item_type, got = parse_item_plaintext(world["crypto"].decrypt(frame["payload"]))
    assert (item_type, got["content"]) == ("comment", "hello")


async def test_publish_item_refuses_a_type_our_cert_cannot_carry(world):
    svc = world["svc"]
    svc._writer_certs.scope = "comment"
    assert (
        await svc.publish_item(SPACE_ID, "post_delete", {"x": 1}, [world["conn"]]) == []
    )
    assert await _queued(world) == []


async def test_schedule_item_publishes_in_the_background(world):
    svc = world["svc"]
    assert svc.schedule_item(SPACE_ID, AUTHOR, "reaction_add", {"emoji": "x"})
    await svc.wait_idle()
    frame = (await _queued(world))[0].sealed
    item_type, _inner = parse_item_plaintext(world["crypto"].decrypt(frame["payload"]))
    assert item_type == "reaction_add"

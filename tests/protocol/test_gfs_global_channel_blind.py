"""§27.9 release blocker (v_96): a GLOBAL space's opaque channel is as blind
as a private one — and it is the only way a household's space content
reaches a connection server.

Since v_96 a global space's members and followers all hold a channel seat,
and a member publishes its items into the channel (the space-keyed
``/gfs/member-publish-anon`` and ``/gfs/publish`` are gone from the
household). Drives a real GFS app end to end with the households' real
:class:`GfsChannelService` and :class:`GfsMemberPublishService` (the world of
:mod:`tests.protocol.test_gfs_private_channel_blind`, with the space made
GLOBAL). Checked over every request (URL and body, every session), the
server's log records, its database file and the fan-out frames:

* the space id, its name, its authority key and the publishing household's
  id never reach the server; every request hits a channel route;
* every item type rides the same generic ``space_item`` under one padded
  size bucket — never the real type, a target id, content, emoji, stamps;
* the writer cert and its user binding stay inside the ciphertext;
* a v_60 ``author_profile`` (a member's name, bio, picture) is ciphertext
  like any other item.

These assertions moved here from ``test_gfs_member_publish_blind`` (they
drove the removed space-keyed household sender); that file keeps the
GFS-side checks of the legacy route until the GFS drops it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode
from socialhome.domain.federation import GfsConnection
from socialhome.domain.gfs_channel import (
    CHANNEL_FRAME_KEYS,
    CHANNEL_PUBLISH_ANON_ROUTE,
    CHANNEL_ROUTES,
    GFS_KEY_FIELD,
    PUBLISH_ANON_KEYS,
    RING_POSITION_KEYS,
)
from socialhome.domain.space import JoinMode, SpaceFeatures, SpaceType
from socialhome.domain.space_author_profile import (
    PROFILE_PICTURE_FIELD,
    encode_picture_sidecar,
    encode_profile_content,
    profile_picture_digest,
)
from socialhome.domain.space_item import ITEM_SIZE_BUCKETS
from socialhome.global_server.app_keys import (
    gfs_envelope_queue_repo_key,
    gfs_fan_out_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.gfs_channel_service import GfsChannelService
from socialhome.services.gfs_member_publish_service import GfsMemberPublishService
from tests.protocol import test_gfs_private_channel_blind as pc

pytestmark = pytest.mark.security

SPACE_ID = pc.SPACE_ID
SPACE_NAME = pc.SPACE_NAME
AUTHOR = pc.AUTHOR
SECRET = "Grandma's secret dumpling recipe"


class _Gfs(pc._Gfs):
    async def subscriber_seats_supported(self, conn):
        return True


def _global_space(owner: str):
    return replace(
        pc._space(owner),
        features=SpaceFeatures(allow_subscribers=True),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )


def _node(h, *, seed, session, conn, peers, seats, link, owner_id):
    spaces = pc._Spaces(_global_space(owner_id), seed=seed)
    certs = pc._Certs(h, peers)
    gfs = _Gfs(session)
    channels = GfsChannelService(
        gfs=gfs,  # type: ignore[arg-type]
        conn_repo=pc._Conns(conn),  # type: ignore[arg-type]
        space_repo=spaces,  # type: ignore[arg-type]
        space_key_repo=pc._Keys(),  # type: ignore[arg-type]
        remote_member_repo=pc._Remote(seats),  # type: ignore[arg-type]
        federation_repo=pc._FedRepo(link),  # type: ignore[arg-type]
        space_crypto=pc._Crypto(),  # type: ignore[arg-type]
        writer_certs=certs,  # type: ignore[arg-type]
        own_instance_id=h.instance_id,
        own_identity_seed=h.seed,
        key_manager=KeyManager(os.urandom(32)),
    )
    channels.attach_federation(pc._Federation())  # type: ignore[arg-type]
    member = GfsMemberPublishService(
        conn_repo=pc._Conns(conn),  # type: ignore[arg-type]
        space_repo=spaces,  # type: ignore[arg-type]
        space_crypto=pc._Crypto(),  # type: ignore[arg-type]
        writer_certs=certs,  # type: ignore[arg-type]
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
        instance_id=pc.GFS_ID,
        cluster_enabled=False,
        cluster_node_id=pc.GFS_ID,
        cluster_peers=(),
    )
    app = create_gfs_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        owner_h, a_h, b_h = pc._Household(), pc._Household(), pc._Household()
        fed = app[gfs_fed_repo_key]
        for h in (owner_h, a_h, b_h):
            await fed.upsert_instance(
                ClientInstance(
                    instance_id=h.instance_id,
                    display_name="h",
                    public_key=h.pk.hex(),
                    status="active",
                )
            )
            await fed.mark_relay_seen(h.instance_id, at=2**31)
        conn = GfsConnection(
            id="conn-1",
            gfs_instance_id=pc.GFS_ID,
            display_name="GFS",
            public_key="00" * 32,
            inbox_url=str(tc.make_url("")).rstrip("/"),
            status="active",
            paired_at="",
        )
        requests: list = []
        common = {
            "session": pc._Session(tc.session, requests),
            "conn": conn,
            "peers": {a_h.instance_id: a_h.pk, b_h.instance_id: b_h.pk},
            "seats": {a_h.instance_id, b_h.instance_id},
            # a is a follower / link-joined household, b a paired member.
            "link": {a_h.instance_id},
            "owner_id": owner_h.instance_id,
        }
        owner = _node(owner_h, seed=pc.SPACE_SEED, **common)
        owner.spaces.instances = [a_h.instance_id, b_h.instance_id]
        a = _node(a_h, seed=None, **common)
        b = _node(b_h, seed=None, **common)
        for node in (owner, a, b):
            await node.channels.start()
            await node.member.start()
        assert await owner.channels.reconcile(SPACE_ID) == "created"
        await owner.channels.announce_epoch(SPACE_ID)
        for node in (a, b):
            raw = await owner.channels.grant_for_peer(SPACE_ID, node.h.instance_id)
            assert raw is not None
            assert await node.channels.accept_grant(SPACE_ID, raw)
            await node.channels.wait_idle()
        yield SimpleNamespace(
            app=app,
            owner=owner,
            a=a,
            b=b,
            requests=requests,
            caplog=caplog,
            tmp=tmp_dir,
        )
        for node in (owner, a, b):
            await node.member.stop()
            await node.channels.stop()


def _leaks() -> tuple[str, ...]:
    return pc._leaks()


async def _publish(world, item_type: str, inner: dict) -> list:
    """b publishes one item into the global channel; a's seat queues it."""
    targets = await world.b.member.plan_item(SPACE_ID, AUTHOR, item_type)
    assert targets, "a global space with a channel publishes into it"
    assert await world.b.member.publish_item(SPACE_ID, item_type, inner, targets)
    await world.app[gfs_fan_out_key].wait_idle()
    return await world.app[gfs_envelope_queue_repo_key].list_for(
        world.a.h.instance_id, now=0
    )


def _server_lines(world) -> str:
    return "\n".join(
        r.getMessage()
        for r in world.caplog.records
        if r.name.startswith("socialhome.global_server")
    )


def _seen(world, queued) -> list[tuple[str, str]]:
    return [
        ("the requests", json.dumps(world.requests, ensure_ascii=False)),
        (
            "the fan-out frame / queued row",
            json.dumps([q.sealed for q in queued], ensure_ascii=False),
        ),
        ("the GFS logs", _server_lines(world)),
    ]


async def test_the_gfs_never_learns_the_global_space_or_the_publisher(world):
    queued = await _publish(
        world, "post", {"post_id": "p-1", "space_id": SPACE_ID, "content": SECRET}
    )
    channel_id = world.owner.spaces.channel[0]
    assert len(queued) == 1
    assert set(queued[0].sealed) == CHANNEL_FRAME_KEYS | RING_POSITION_KEYS
    assert queued[0].sealed["channel_id"] == channel_id
    publishes = 0
    for method, url, body in world.requests:
        text = url + json.dumps(body, sort_keys=True)
        for leak in (*_leaks(), SECRET):
            assert leak not in text, (method, url, leak)
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        # Only channel routes — never /gfs/publish, /gfs/member-publish-anon,
        # /gfs/spaces/{id}/epoch or /gfs/subscribe.
        assert path in CHANNEL_ROUTES, path
        assert channel_id not in path
        if path == CHANNEL_PUBLISH_ANON_ROUTE:
            # The publish itself is anonymous: it names no household (a
            # seat subscribe is identity-bound by design, the channel's
            # documented trade-off; it names no space).
            publishes += 1
            assert world.b.h.instance_id not in text
    assert publishes == 1
    assert world.b.h.instance_id not in json.dumps(queued[0].sealed)
    for line in _server_lines(world).splitlines():
        for leak in (*_leaks(), SECRET):
            assert leak not in line, line
    dump = b"".join(p.read_bytes() for p in world.tmp.glob("gfs.db*"))
    for leak in (*_leaks(), SECRET):
        assert leak.encode() not in dump


ITEM_TYPES = (
    "post",
    "post_edit",
    "post_delete",
    "comment",
    "comment_edit",
    "comment_delete",
    "reaction_add",
    "reaction_remove",
)


@pytest.mark.parametrize("item_type", ITEM_TYPES)
async def test_no_member_item_type_ever_reaches_the_gfs_in_plaintext(world, item_type):
    """Every item type rides the same generic frame under one 1 KiB padded
    bucket: the requests, the fan-out frame and the GFS logs carry neither
    the real type nor any of the item's fields."""
    secrets = {
        "item_target": "target-id-1f3a",
        "post_id": "post-id-9b2c",
        "content": SECRET,
        "emoji": "🦄",
        "ts": "2026-10-03T12:34:56.789012+00:00",
        "edited_at": "2026-10-03T12:34:57.123456+00:00",
        "parent_id": "parent-id-77aa",
    }
    queued = await _publish(world, item_type, {"item_type": item_type, **secrets})
    assert len(queued) == 1
    assert len(queued[0].sealed["payload"]) == len(
        b64url_encode(bytes(12 + ITEM_SIZE_BUCKETS[0] + 16))
    )
    for where, blob in _seen(world, queued):
        assert f'"{item_type}"' not in blob, f"{where} carries the item type"
        assert "item_type" not in blob, f"{where} carries an item_type field"
        for leak in secrets.values():
            assert leak not in blob, f"{where} carries {leak!r}"


async def test_the_writer_cert_and_its_user_binding_never_reach_the_gfs(world):
    queued = await _publish(
        world, "post", {"post_id": "p-2", "space_id": SPACE_ID, "content": "x"}
    )
    for where, blob in _seen(world, queued):
        for leak in (AUTHOR, "writer_user_ids", "users_sig", "writer_cert"):
            assert leak not in blob, f"{where} carries {leak!r}"


async def test_an_author_profile_reaches_the_gfs_with_no_name_bio_or_picture(world):
    picture = b"RIFF\x00\x00\x00\x00WEBPVP8 " + os.urandom(600)
    content, _rev = encode_profile_content(
        display_name="Noor Haddad",
        bio="Beekeeper on a Höngg rooftop.",
        picture_hash=profile_picture_digest(picture),
    )
    inner = {
        "item_type": "author_profile",
        "item_target": AUTHOR,
        "post_id": AUTHOR,
        "author_user_id": AUTHOR,
        "author_username": "noor",
        "content": content,
        PROFILE_PICTURE_FIELD: encode_picture_sidecar(picture),
    }
    queued = await _publish(world, "author_profile", inner)
    assert len(queued) == 1
    for where, blob in _seen(world, queued):
        for leak in (
            "Noor Haddad",
            "Haddad",
            "Beekeeper",
            "author_profile",
            "display_name",
            AUTHOR,
            content[:40],
            encode_picture_sidecar(picture)[:40],
        ):
            assert leak not in blob, f"{where} carries {leak!r}"


def _channel_publishes(requests: list) -> list[dict]:
    return [b for _m, u, b in requests if u.endswith(CHANNEL_PUBLISH_ANON_ROUTE)]


async def test_nothing_identifying_reaches_the_gfs_for_a_member_publish(world):
    """(Moved from ``test_gfs_member_publish_strict_blind``.) The publish
    request and the fan-out frame carry no household id or key, no
    household signature, no writer cert and no user it binds."""
    world.requests.clear()
    world.caplog.clear()
    queued = await _publish(
        world, "post", {"post_id": "p-3", "space_id": SPACE_ID, "content": "x"}
    )
    [body] = _channel_publishes(world.requests)
    # Exactly the anonymous request shape: nothing more can ride along.
    assert PUBLISH_ANON_KEYS <= set(body) <= PUBLISH_ANON_KEYS | GFS_KEY_FIELD
    h = world.b.h
    for where, blob in (
        ("the request", json.dumps(body)),
        ("the fan-out frame / queued row", json.dumps(queued[0].sealed)),
        ("the GFS logs during the publish", _server_lines(world)),
    ):
        for leak in (
            h.instance_id,
            h.pk.hex(),
            b64url_encode(h.pk),
            AUTHOR,
            "writer_cert",
            "instance_pk",
            "writer_user_ids",
            '"signature"',
        ):
            assert leak not in blob, f"{where} carries {leak!r}"


async def test_two_households_publish_indistinguishable_requests(world):
    """(Moved from ``test_gfs_member_publish_strict_blind``.) Two member
    households' channel publishes differ only in per-request randomness —
    nothing in the request depends on WHICH household sent it."""
    world.requests.clear()
    inner = {"post_id": "p-4", "space_id": SPACE_ID, "content": "x"}
    for node in (world.a, world.b):
        targets = await node.member.plan_item(SPACE_ID, AUTHOR, "post")
        assert targets
        assert await node.member.publish_item(SPACE_ID, "post", inner, targets)
    a, b = _channel_publishes(world.requests)
    assert set(a) == set(b)
    varying = {k for k in a if a[k] != b[k] and k not in ("payload",)}
    # Only stamps, nonces and signatures vary — never an identity field.
    assert varying <= {"ts", "nonce", "writer_sig", "sig"}, varying
    assert len(a["payload"]) == len(b["payload"])
    for node, other in ((world.a, b), (world.b, a)):
        for leak in (node.h.instance_id, node.h.pk.hex()):
            assert leak not in json.dumps(other)

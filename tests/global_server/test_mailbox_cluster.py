"""GFS mailboxes across a cluster: two real nodes on ONE database (the
production shape). A mailbox registered on node A and claimed on node B
gets the frames posted to node A — live through the shared queue and the
``NODE_DRAIN_HINT``, and queued while offline then drained on the claim."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from socialhome.domain.relay_media_frame import (
    VALID_SEALED_LENGTHS,
    build_relay_media_body,
)
from socialhome.federation.gfs_mailbox import (
    MailboxClaim,
    derive_mailbox_keys,
    register_mailbox,
)
from socialhome.global_server import cluster as cluster_mod
from socialhome.global_server.app_keys import (
    gfs_cluster_key,
    gfs_cluster_repo_key,
    gfs_envelope_queue_repo_key,
    gfs_mailbox_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClusterNode
from socialhome.global_server.server import create_gfs_app

GFS_ID = "gfs-shared"
SEALED = {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": "Y2lwaGVy"}


def _config(data_dir, node_id: str) -> GfsConfig:
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://127.0.0.1:1",
        data_dir=str(data_dir),
        instance_id=GFS_ID,
        cluster_enabled=True,
        cluster_node_id=node_id,
        cluster_peers=(),
    )


@pytest.fixture
async def nodes(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_DELAY_S", 0.01)
    shared = tmp_path_factory.mktemp("gfs-mailbox-shared")
    a = TestServer(create_gfs_app(_config(shared, "node-a")))
    await a.start_server()
    b = TestServer(create_gfs_app(_config(shared, "node-b")))
    await b.start_server()
    own_key = a.app[gfs_cluster_key].own_public_key_hex
    repo = a.app[gfs_cluster_repo_key]
    for node_id, server in (("node-a", a), ("node-b", b)):
        await repo.insert_node(
            ClusterNode(
                node_id=node_id,
                url=str(server.make_url("")).rstrip("/"),
                public_key=own_key,
                status="online",
            )
        )
    async with aiohttp.ClientSession() as session:
        try:
            yield a, b, session
        finally:
            await b.close()
            await a.close()


def _url(server: TestServer) -> str:
    return str(server.make_url("")).rstrip("/")


def _media(to: str, fill: int) -> bytes:
    size = min(VALID_SEALED_LENGTHS)
    return build_relay_media_body(to, bytes([fill]) * size)


async def _post(session, server, *, json=None, data=None) -> None:
    async with session.post(_url(server) + "/gfs/envelope", json=json, data=data) as r:
        assert r.status == 202


async def _wait(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never held")


async def test_register_on_a_claim_on_b_live_frames_posted_on_a(nodes):
    a, b, session = nodes
    keys = derive_mailbox_keys(b"\x05" * 32, GFS_ID, "space-x", 0)
    await register_mailbox(session, _url(a), keys, GFS_ID)
    async with MailboxClaim(session, _url(b), keys, GFS_ID) as claim:
        assert b.app[gfs_mailbox_ws_registry_key].is_connected(keys.mailbox_id)
        assert not a.app[gfs_mailbox_ws_registry_key].is_connected(keys.mailbox_id)
        await _post(session, a, json={"to_instance": keys.mailbox_id, "sealed": SEALED})
        assert await claim.receive(timeout=5) == {"type": "envelope", "sealed": SEALED}
        media = _media(keys.mailbox_id, 3)
        await _post(session, a, data=media)
        assert await claim.receive(timeout=5) == media
    queue = a.app[gfs_envelope_queue_repo_key]

    async def _empty() -> bool:
        return await queue.count_for(keys.mailbox_id) == 0

    await _wait(_empty)


async def test_queued_while_offline_then_drained_on_a_claim_on_b(nodes):
    a, b, session = nodes
    keys = derive_mailbox_keys(b"\x06" * 32, GFS_ID, "space-y", 2)
    await register_mailbox(session, _url(a), keys, GFS_ID)
    media = _media(keys.mailbox_id, 4)
    await _post(session, a, json={"to_instance": keys.mailbox_id, "sealed": SEALED})
    await _post(session, a, data=media)
    queue = a.app[gfs_envelope_queue_repo_key]

    async def _queued() -> bool:
        return await queue.count_for(
            keys.mailbox_id
        ) == 1 and await queue.media_bytes_for(keys.mailbox_id, now=0) == len(media)

    await _wait(_queued)
    async with MailboxClaim(session, _url(b), keys, GFS_ID) as claim:
        assert await claim.receive(timeout=5) == {"type": "envelope", "sealed": SEALED}
        assert await claim.receive(timeout=5) == media

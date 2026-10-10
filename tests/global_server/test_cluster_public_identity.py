"""Two shared-seed GFS allocs behind one hostname: one public identity.

The production shape: N allocs share one identity seed, one database and one
``base_url`` behind a round-robin load balancer, so a household's
``GET /gfs/info`` and its next request usually land on DIFFERENT nodes. The
``gfs_instance_id`` it pinned from the first is signed into the second as the
addressee — so every node must answer to the same id (or, while migrating,
accept the old per-node ids as aliases). These tests run two real nodes on
one database and send the info fetch and the publish to different ones.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    sign_ed25519,
)
from socialhome.domain.gfs_member_publish import MemberPublishRequest
from socialhome.global_server.app_keys import gfs_cluster_key, gfs_fed_repo_key
from socialhome.global_server.cluster import NODE_HELLO
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.writer_cert import sign_writer_cert

SPACE_ID = "sp-public"
SEED_HEX = "5e" * 32
SPACE_SEED = os.urandom(32)
HOUSEHOLD_SEED = os.urandom(32)
HOUSEHOLD_PK = ed25519_public_key(HOUSEHOLD_SEED)
HOUSEHOLD_ID = derive_instance_id(HOUSEHOLD_PK)


def _config(
    data_dir: Path, *, node_id: str, instance_id: str, aliases: tuple[str, ...]
) -> GfsConfig:
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://127.0.0.1:1",
        data_dir=str(data_dir),
        instance_id=instance_id,
        instance_id_aliases=aliases,
        # The shared identity seed every alloc of the deployment is given.
        signing_seed_hex=SEED_HEX,
        # Cluster mode stays off so the nodes do not gossip on their own;
        # the HELLO test below drives the frame itself.
        cluster_enabled=False,
        cluster_node_id=node_id,
    )


async def _node(
    data_dir: Path, *, node_id: str, instance_id: str, aliases=()
) -> TestServer:
    app = create_gfs_app(
        _config(data_dir, node_id=node_id, instance_id=instance_id, aliases=aliases)
    )
    server = TestServer(app)
    await server.start_server()
    app[gfs_cluster_key]._self_url = _url(server)
    return server


def _url(server: TestServer) -> str:
    return str(server.make_url("")).rstrip("/")


async def _seed_state(server: TestServer) -> None:
    """The household and the space, in the database both nodes share."""
    fed = server.app[gfs_fed_repo_key]
    await fed.upsert_instance(
        ClientInstance(
            instance_id=HOUSEHOLD_ID,
            display_name="H",
            public_key=HOUSEHOLD_PK.hex(),
            status="active",
        )
    )
    await fed.upsert_space(
        GlobalSpace(
            space_id=SPACE_ID,
            owning_instance=HOUSEHOLD_ID,
            name="Public",
            allow_subscribers=True,
            status="active",
            identity_public_key=ed25519_public_key(SPACE_SEED).hex(),
        )
    )
    await fed.mark_relay_seen(HOUSEHOLD_ID, at=int(time.time()))


def _publish_body(gfs_instance_id: str, payload: str) -> dict:
    req = MemberPublishRequest(
        instance_id=HOUSEHOLD_ID,
        gfs_instance_id=gfs_instance_id,
        ts=datetime.now(timezone.utc).isoformat(),
        signature="",
        target=SPACE_ID,
        epoch=3,
        writer_cert=sign_writer_cert(
            space_seed=SPACE_SEED,
            space_id=SPACE_ID,
            epoch=3,
            instance_pk=HOUSEHOLD_PK,
            scope="comment",
        ),
        payload=payload,
    )
    sig = sign_ed25519(HOUSEHOLD_SEED, req.signing_bytes())
    return replace(req, signature=b64url_encode(sig)).to_wire()


async def _info(server: TestServer) -> dict:
    async with ClientSession() as http:
        async with http.get(f"{_url(server)}/gfs/info") as resp:
            assert resp.status == 200
            return await resp.json()


async def _publish(server: TestServer, gfs_instance_id: str, payload: str) -> int:
    async with ClientSession() as http:
        async with http.post(
            f"{_url(server)}/gfs/member-publish",
            json=_publish_body(gfs_instance_id, payload),
        ) as resp:
            return resp.status


async def _healthz(server: TestServer) -> int:
    async with ClientSession() as http:
        async with http.get(f"{_url(server)}/healthz") as resp:
            return resp.status


@pytest.fixture
async def pair(tmp_dir: Path, request) -> AsyncIterator[tuple[TestServer, TestServer]]:
    """Two allocs on ONE data dir (one database, one seed): ``(a, b)``.

    ``request.param`` is ``(id_a, id_b, aliases)`` or
    ``(id_a, id_b, aliases_a, aliases_b)``."""
    id_a, id_b, *aliases = request.param
    aliases_a = aliases[0]
    aliases_b = aliases[-1]
    a = await _node(tmp_dir, node_id="gfs-0", instance_id=id_a, aliases=aliases_a)
    b = await _node(tmp_dir, node_id="gfs-1", instance_id=id_b, aliases=aliases_b)
    try:
        await _seed_state(a)
        yield a, b
    finally:
        await a.close()
        await b.close()


@pytest.mark.parametrize("pair", [("gfs-shared", "gfs-shared", ())], indirect=True)
async def test_a_shared_instance_id_serves_households_on_any_node(pair):
    a, b = pair
    info = await _info(a)
    # Pinned from node a, signed into a request node b receives: accepted.
    assert await _publish(b, info["gfs_instance_id"], "item-1") == 200
    assert await _publish(a, info["gfs_instance_id"], "item-2") == 200
    assert (await _info(b))["gfs_instance_id"] == info["gfs_instance_id"]


@pytest.mark.parametrize("pair", [("gfs-0", "gfs-1", ())], indirect=True)
async def test_per_node_instance_ids_refuse_requests_routed_to_another_node(pair):
    """The production bug: each alloc served its own id, so whatever node
    answered the household's info fetch, the balancer sent most requests to
    another one, which refused them as addressed elsewhere."""
    a, b = pair
    pinned = (await _info(a))["gfs_instance_id"]
    assert pinned == "gfs-0"
    assert await _publish(b, pinned, "item-1") == 403


@pytest.mark.parametrize(
    "pair", [("gfs-shared", "gfs-shared", ("gfs-0", "gfs-1"))], indirect=True
)
async def test_during_migration_an_old_per_node_id_is_accepted_on_every_node(pair):
    """After the switch to one shared id: a household that pinned an old
    per-node id is served by every node through the aliases, and what it
    reads from /gfs/info on its next reconnect lets it rebind — the same
    key, a capability block that verifies under (that key, the new id)."""
    a, b = pair
    for node in (a, b):
        for old in ("gfs-0", "gfs-1"):
            assert await _publish(node, old, f"{old}-via-{_url(node)}") == 200
    info_a, info_b = await _info(a), await _info(b)
    assert info_a["gfs_instance_id"] == info_b["gfs_instance_id"] == "gfs-shared"
    assert info_a["public_key"] == info_b["public_key"]
    assert verify_capabilities(
        info_a["public_key"],
        "gfs-shared",
        info_b["capabilities"],
        info_b["capabilities_sig"],
        info_b["capabilities_sig_suite"],
    )
    # The aliases are served only inside the SIGNED block, as ``replaces`` —
    # what lets a household pinned to one of them move to the shared id.
    assert info_a["capabilities"]["replaces"] == ["gfs-0", "gfs-1"]


async def _hello(a: TestServer, b: TestServer) -> None:
    """A real NODE_HELLO from node a to node b over /cluster/sync."""
    cluster_a = a.app[gfs_cluster_key]
    await cluster_a._post_to_peer(
        _url(b), NODE_HELLO, cluster_a._hello_payload(), to="", session=None
    )


@pytest.mark.parametrize("pair", [("gfs-0", "gfs-1", ())], indirect=True)
async def test_per_node_ids_are_flagged_but_never_fail_healthz(pair):
    """Today's per-alloc cluster run on this build (an image-only upgrade):
    flagged at ERROR in the admin view, but /healthz stays 200 — failing it
    would pull every node out of the balancer."""
    a, b = pair
    await _hello(a, b)
    assert await _healthz(b) == 200
    view = await b.app[gfs_cluster_key].admin_cluster()
    assert view["instance_id_mismatches"] == [
        {"node_id": "gfs-0", "instance_id": "gfs-0"}
    ]


@pytest.mark.parametrize(
    "pair", [("gfs-0", "gfs-shared", (), ("gfs-0", "gfs-1"))], indirect=True
)
async def test_a_rolling_instance_id_change_is_transitional_on_both_sides(pair):
    """Node a still runs the old per-node id; node b already serves the
    shared id with the old ids as aliases. Each side sees the other as a
    rolling change (linked through b's aliases), not a mismatch."""
    a, b = pair
    await _hello(a, b)
    await _hello(b, a)
    for node, other, other_id in ((a, "gfs-1", "gfs-shared"), (b, "gfs-0", "gfs-0")):
        view = await node.app[gfs_cluster_key].admin_cluster()
        assert view["instance_id_mismatches"] == []
        assert view["instance_id_transitional"] == [
            {"node_id": other, "instance_id": other_id}
        ]
        assert await _healthz(node) == 200


@pytest.mark.parametrize("pair", [("gfs-shared", "gfs-shared", ())], indirect=True)
async def test_a_sibling_hello_with_the_same_instance_id_flags_nothing(pair):
    a, b = pair
    await _hello(a, b)
    view = await b.app[gfs_cluster_key].admin_cluster()
    assert view["instance_id_mismatches"] == view["instance_id_transitional"] == []

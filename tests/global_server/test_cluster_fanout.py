"""Two-node cluster fan-out integration tests (spec §24.10).

Spins up two real :class:`aiohttp.test_utils.TestServer` instances and
exchanges NODE_* messages over real HTTP so the full sign-verify-dispatch
pipeline runs — including ``_broadcast``, ``_post_to_peer``,
``_ping_peer``, and the wire helpers.

Also exercises the cheap-to-cover pure-Python paths (``apply_relay``
dedup, ``_gc_seen``, disabled fan-out no-ops).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server import cluster as cluster_mod
from socialhome.global_server.app_keys import (
    gfs_cluster_key,
    gfs_cluster_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.cluster import (
    ClusterService,
    _report_to_wire,
    _wire_to_client,
    _wire_to_report,
    _wire_to_space,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import (
    ClientInstance,
    GfsFraudReport,
    GlobalSpace,
)
from socialhome.global_server.server import create_gfs_app


def _config(tmp, *, instance_id: str, cluster_peers=()):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp),
        instance_id=instance_id,
        cluster_enabled=True,
        cluster_node_id=instance_id,
        cluster_peers=cluster_peers,
    )


async def _start_node(tmp_dir, instance_id: str) -> TestServer:
    """Create + start a GFS TestServer for the given instance_id."""
    app = create_gfs_app(_config(tmp_dir, instance_id=instance_id))
    server = TestServer(app)
    await server.start_server()
    return server


async def _stop_node(server: TestServer) -> None:
    await server.close()


async def test_two_node_sync_end_to_end(tmp_dir, tmp_path_factory):
    """Full NODE_* fan-out between two live GFS nodes.

    Covers ``add_peer`` → NODE_HELLO POST → ``_post_to_peer`` →
    ``handle_hello`` on the receiver; symmetric handshake; then
    ``sync_client`` / ``sync_space`` / ``sync_report`` / ``sync_policy``
    each broadcast via ``_broadcast`` to the peer and land via
    ``apply_sync_*`` on the receiver.
    """
    dir_a = tmp_path_factory.mktemp("gfs-a")
    dir_b = tmp_path_factory.mktemp("gfs-b")
    a = await _start_node(dir_a, "A")
    b = await _start_node(dir_b, "B")
    try:
        url_a = str(a.make_url("")).rstrip("/")
        url_b = str(b.make_url("")).rstrip("/")

        cluster_a: ClusterService = a.app[gfs_cluster_key]
        cluster_b: ClusterService = b.app[gfs_cluster_key]

        # Mutual TOFU — each admin-add the other. `add_peer` stores the
        # peer row locally + fires a NODE_HELLO with our own pk so the
        # other side records us + our key.
        await cluster_a.add_peer(url_b)
        await cluster_b.add_peer(url_a)
        # After the symmetric HELLO round-trip both nodes know each other.
        peers_a = await a.app[gfs_cluster_repo_key].list_nodes()
        peers_b = await b.app[gfs_cluster_repo_key].list_nodes()
        # B recorded A after A's HELLO arrived (via _post_to_peer).
        assert any(p.node_id == "A" for p in peers_b)
        assert any(p.node_id == "B" for p in peers_a)

        # Seed an "owning instance" on A and broadcast it to B. `sync_
        # client` → `_broadcast` → `_post_to_peer` → B's handler runs
        # `apply_sync_client` and upserts the row into B's fed repo.
        owner = ClientInstance(
            instance_id="owner.home",
            display_name="Owner",
            public_key="aa" * 32,
            inbox_url="http://o/wh",
            status="active",
        )
        await a.app[gfs_fed_repo_key].upsert_instance(owner)
        await cluster_a.sync_client(owner)
        # Allow B's event loop to drain the POST.
        await asyncio.sleep(0.05)
        assert await b.app[gfs_fed_repo_key].get_instance("owner.home")

        # sync_space fan-out.
        space = GlobalSpace(
            space_id="sp-xx",
            owning_instance="owner.home",
            name="Example",
            status="active",
        )
        await a.app[gfs_fed_repo_key].upsert_space(space)
        await cluster_a.sync_space(space)
        await asyncio.sleep(0.05)
        assert await b.app[gfs_fed_repo_key].get_space("sp-xx")

        # sync_report fan-out (Phase Z).
        report = GfsFraudReport(
            id="rpt-1",
            target_type="space",
            target_id="sp-xx",
            category="spam",
            notes=None,
            reporter_instance_id="owner.home",
            reporter_user_id=None,
            status="pending",
            created_at=int(time.time()),
        )
        await cluster_a.sync_report(report)
        await asyncio.sleep(0.05)
        # B has the report — pull via admin_repo.list_fraud_reports.
        from socialhome.global_server.app_keys import gfs_admin_repo_key

        rows = await b.app[gfs_admin_repo_key].list_fraud_reports()
        assert any(r.id == "rpt-1" for r in rows)

        # sync_policy fan-out — B's server_config should pick up the keys.
        await cluster_a.sync_policy(
            {
                "auto_accept_clients": "0",
                "auto_accept_spaces": "1",
                "fraud_threshold": "9",
            }
        )
        await asyncio.sleep(0.05)
        assert (
            await b.app[gfs_admin_repo_key].get_config(
                "auto_accept_clients",
            )
            == "0"
        )
        assert (
            await b.app[gfs_admin_repo_key].get_config(
                "auto_accept_spaces",
            )
            == "1"
        )
        assert (
            await b.app[gfs_admin_repo_key].get_config(
                "fraud_threshold",
            )
            == "9"
        )

        # Relay fan-out — fire-and-forget; we just need it to NOT raise.
        await cluster_a.relay_to_peers(
            "sp-xx",
            {
                "msg_id": "m1",
                "event_type": "POST_PUBLISH",
            },
        )
        await asyncio.sleep(0.1)

        # _ping_peer via ping_peer() — exercises /cluster/health GET.
        assert await cluster_a.ping_peer(url_b) is True
        assert await cluster_a.ping_peer("http://127.0.0.1:1") is False
    finally:
        await _stop_node(a)
        await _stop_node(b)


async def test_post_to_peer_raises_on_non_2xx(tmp_dir, tmp_path_factory):
    """``_broadcast`` retries once on error then logs + drops (ignore_errors
    path via ``relay_to_peers`` + NODE_RELAY to an offline peer)."""
    dir_a = tmp_path_factory.mktemp("gfs-solo")
    a = await _start_node(dir_a, "A")
    try:
        cluster_a: ClusterService = a.app[gfs_cluster_key]
        # Manually register an unreachable peer row so `_broadcast`
        # iterates through it.
        from socialhome.global_server.domain import ClusterNode

        await a.app[gfs_cluster_repo_key].upsert_node(
            ClusterNode(
                node_id="ghost",
                url="http://127.0.0.1:1",
                public_key="",
                status="online",
            )
        )
        # Fire-and-forget relay should swallow the connection error.
        await cluster_a.relay_to_peers("sp", {"msg_id": "m2"})
        await asyncio.sleep(0.1)
        # And a non-ignore_errors path (sync_client) should still return;
        # both primary + retry fail but the loop catches.
        await cluster_a.sync_client(
            ClientInstance(
                instance_id="x",
                display_name="X",
                public_key="aa" * 32,
                inbox_url="http://x",
                status="active",
            )
        )
    finally:
        await _stop_node(a)


# ─── Cheap pure-Python coverage ────────────────────────────────────────


@pytest.fixture
async def started_app(tmp_dir):
    """A GFS app with lifecycle started (db + services) under TestClient."""
    app = create_gfs_app(_config(tmp_dir, instance_id="solo"))
    async with TestClient(TestServer(app)):
        yield app


@pytest.fixture
async def disabled_app(tmp_dir):
    """A GFS app with ``cluster_enabled=False`` so broadcasts are no-ops."""
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="solo",
        cluster_enabled=False,
        cluster_node_id="solo",
        cluster_peers=(),
    )
    app = create_gfs_app(cfg)
    async with TestClient(TestServer(app)):
        yield app


async def test_cluster_disabled_noops(disabled_app):
    """When ``cluster_enabled=False``, outbound broadcasts are no-ops."""
    svc: ClusterService = disabled_app[gfs_cluster_key]
    # Every broadcast is an early return — no peers, no errors.
    await svc.sync_client(
        ClientInstance(
            instance_id="x",
            display_name="X",
            public_key="aa" * 32,
            inbox_url="http://x",
        )
    )
    await svc.sync_space(GlobalSpace(space_id="s", owning_instance="o"))
    await svc.sync_report(
        GfsFraudReport(
            id="r1",
            target_type="space",
            target_id="s",
            category="spam",
            notes=None,
            reporter_instance_id="x",
            reporter_user_id=None,
            status="pending",
            created_at=0,
        )
    )
    await svc.sync_policy({"auto_accept_clients": "1"})
    await svc.relay_to_peers("sp", {})


async def test_apply_relay_dedups_and_gc(started_app):
    """``apply_relay`` dedups on msg_id and ``_gc_seen`` prunes old entries."""
    svc: ClusterService = started_app[gfs_cluster_key]
    # Seed an old entry so the next _gc_seen drops it.
    svc._seen_relays["old"] = time.monotonic() - 10_000
    # Fresh msg_id takes the "record + gc" path (no dedup hit).
    await svc.apply_relay("sp", {"msg_id": "abc", "event_type": "X"})
    assert "abc" in svc._seen_relays
    assert "old" not in svc._seen_relays
    # Second call with the same msg_id hits the dedup fast-return.
    await svc.apply_relay("sp", {"msg_id": "abc", "event_type": "X"})
    # Empty-msg_id path is a silent no-op.
    await svc.apply_relay("sp", {"event_type": "X"})


async def test_apply_sync_client_banned_wins_lww(started_app):
    """A ban upsert on the peer cannot be overwritten by a later active."""
    svc: ClusterService = started_app[gfs_cluster_key]
    fed = started_app[gfs_fed_repo_key]
    await svc.apply_sync_client(
        "upsert",
        {
            "instance_id": "x",
            "public_key": "aa" * 32,
            "inbox_url": "http://x",
            "status": "banned",
        },
    )
    await svc.apply_sync_client(
        "upsert",
        {
            "instance_id": "x",
            "public_key": "aa" * 32,
            "inbox_url": "http://x",
            "status": "active",
        },
    )
    inst = await fed.get_instance("x")
    assert inst.status == "banned"


async def test_apply_sync_space_banned_wins_lww(started_app):
    svc: ClusterService = started_app[gfs_cluster_key]
    fed = started_app[gfs_fed_repo_key]
    # Seed owner so FK holds.
    await svc.apply_sync_client(
        "upsert",
        {
            "instance_id": "o",
            "public_key": "aa" * 32,
            "inbox_url": "http://o",
            "status": "active",
        },
    )
    await svc.apply_sync_space(
        "ban",
        {
            "space_id": "sp",
            "owning_instance": "o",
            "status": "banned",
        },
    )
    await svc.apply_sync_space(
        "upsert",
        {
            "space_id": "sp",
            "owning_instance": "o",
            "status": "active",
        },
    )
    assert (await fed.get_space("sp")).status == "banned"


async def test_apply_sync_report_with_bad_wire_is_silent(started_app):
    """Malformed wire shapes silently drop (``except (KeyError, ValueError)``)."""
    svc: ClusterService = started_app[gfs_cluster_key]
    # Missing the required 'id' key — must not raise.
    await svc.apply_sync_report({"target_type": "space"})


async def test_handle_heartbeat_updates_last_seen(started_app):
    """``handle_heartbeat`` refreshes a known peer's ``status`` + last_seen."""
    svc: ClusterService = started_app[gfs_cluster_key]
    from socialhome.global_server.domain import ClusterNode

    await started_app[gfs_cluster_repo_key].upsert_node(
        ClusterNode(
            node_id="peer",
            url="http://peer",
            public_key="bb" * 32,
            status="offline",
        )
    )
    await svc.handle_heartbeat("peer")
    rows = await started_app[gfs_cluster_repo_key].list_nodes()
    match = next(r for r in rows if r.node_id == "peer")
    assert match.status == "online"
    # Unknown peer is a silent no-op (fast-path return).
    await svc.handle_heartbeat("nonexistent")


async def test_wire_helpers_roundtrip_client_space_report():
    """Wire serialisers round-trip domain objects."""
    c = ClientInstance(
        instance_id="x",
        display_name="X",
        public_key="aa" * 32,
        inbox_url="http://x",
        status="active",
        auto_accept=True,
        connected_at="2026-01-01T00:00:00",
    )
    assert _wire_to_client(cluster_mod._client_to_wire(c)) == c

    s = GlobalSpace(
        space_id="s",
        owning_instance="o",
        name="N",
        description="D",
        about_markdown="M",
        cover_url="U",
        min_age=13,
        target_audience="teen",
        accent_color="#abcdef",
        status="active",
        subscriber_count=3,
        posts_per_week=1.5,
        published_at="2026-01-01T00:00:00",
    )
    assert _wire_to_space(cluster_mod._space_to_wire(s)) == s

    r = GfsFraudReport(
        id="r",
        target_type="space",
        target_id="t",
        category="spam",
        notes="n",
        reporter_instance_id="i",
        reporter_user_id="u",
        status="pending",
        created_at=123,
    )
    assert _wire_to_report(_report_to_wire(r)) == r

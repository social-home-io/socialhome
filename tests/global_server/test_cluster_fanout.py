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
import json
import time
from dataclasses import replace
from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.authority_cert import sign_authority_cert
from socialhome.authority_sig import sign_authority_event
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.domain.gfs_channel import channel_frame
from socialhome.gfs_channel import new_channel_id
from socialhome.global_server import cluster as cluster_mod
from socialhome.global_server import member_publish as mp_mod
from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_channel_repo_key,
    gfs_channel_service_key,
    gfs_cluster_key,
    gfs_cluster_repo_key,
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.cluster import (
    NODE_HEARTBEAT,
    ClusterService,
    _report_to_wire,
    _wire_to_client,
    _wire_to_report,
    _wire_to_space,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import (
    ClientInstance,
    ClusterNode,
    GfsFraudReport,
    GlobalSpace,
)
from socialhome.global_server.server import create_gfs_app

#: Every node advertises this as its own URL in its HELLO, so each also learns
#: a peer row it can never reach (see ``fast_sync_retry``). A closed loopback
#: port refuses at once — ``http://gfs.test`` sent each of those posts out for
#: a real DNS lookup.
_UNREACHABLE_SELF_URL = "http://127.0.0.1:1"


def _config(tmp, *, instance_id: str, cluster_peers=()):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url=_UNREACHABLE_SELF_URL,
        data_dir=str(tmp),
        instance_id=instance_id,
        cluster_enabled=True,
        cluster_node_id=instance_id,
        cluster_peers=cluster_peers,
    )


async def _start_node(tmp_dir, instance_id: str) -> TestServer:
    """Create + start a GFS TestServer for the given instance_id.

    The port is only known once the server listens, so the node's own URL
    (what its HELLO advertises, and where a peer answers it) is set then.
    """
    app = create_gfs_app(_config(tmp_dir, instance_id=instance_id))
    server = TestServer(app)
    await server.start_server()
    app[gfs_cluster_key]._self_url = str(server.make_url("")).rstrip("/")
    return server


async def _stop_node(server: TestServer) -> None:
    await server.close()


async def _approve(here: TestServer, there: TestServer, there_id: str) -> None:
    """Operator approval on *here*: pin *there*'s id, URL and identity key.

    Two test nodes have distinct data dirs, so distinct seeds — each must
    pin the other's key before the other's frames are accepted.
    """
    await here.app[gfs_cluster_key].add_peer(
        there_id,
        str(there.make_url("")).rstrip("/"),
        there.app[gfs_cluster_key].own_public_key_hex,
    )


@pytest.fixture
def fast_sync_retry(monkeypatch):
    """Shrink the production 5 s ``_broadcast`` retry back-off.

    Both nodes advertise the same unreachable ``base_url`` in their HELLO,
    so each also learns a peer row it can never reach, and every fan-out to
    it fails, sleeps ``SYNC_RETRY_DELAY_S``, retries and drops — the
    retry-then-drop path still runs, it just no longer costs 5 s a time
    (the end-to-end test spent 20 of its 20.5 s asleep here).
    """
    monkeypatch.setattr(cluster_mod, "SYNC_RETRY_DELAY_S", 0.01)


async def test_two_node_sync_end_to_end(tmp_dir, tmp_path_factory, fast_sync_retry):
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
        url_b = str(b.make_url("")).rstrip("/")

        cluster_a: ClusterService = a.app[gfs_cluster_key]

        # Each operator approves the other node (id + URL + key). `add_peer`
        # pins the peer row locally + fires a NODE_HELLO so the other side
        # marks us online.
        await _approve(a, b, "B")
        await _approve(b, a, "A")
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


@pytest.mark.parametrize("first", ["A", "B"])
async def test_admin_approval_converges_in_either_order(tmp_path_factory, first):
    """Approving each node on the other converges whichever side the
    operator does first: the first HELLO is refused (the other side has
    not approved us yet), the second is accepted and — the row never seen
    before — answered, so both rows end up online."""
    a = await _start_node(tmp_path_factory.mktemp("conv-a"), "A")
    b = await _start_node(tmp_path_factory.mktemp("conv-b"), "B")
    try:
        nodes = {"A": a, "B": b}
        second = "B" if first == "A" else "A"
        await _approve(nodes[first], nodes[second], second)
        # Not yet approved on the other side: nothing was written there.
        assert await nodes[second].app[gfs_cluster_repo_key].list_nodes() == []
        await _approve(nodes[second], nodes[first], first)
        for here, there_id, there in ((a, "B", b), (b, "A", a)):
            (row,) = await here.app[gfs_cluster_repo_key].list_nodes()
            assert row.node_id == there_id
            assert row.public_key == there.app[gfs_cluster_key].own_public_key_hex
            assert row.status == "online"
            assert row.last_seen is not None
    finally:
        await _stop_node(a)
        await _stop_node(b)


async def test_post_to_peer_raises_on_non_2xx(
    tmp_dir, tmp_path_factory, fast_sync_retry
):
    """``_broadcast`` retries once on error then logs + drops (ignore_errors
    path via ``relay_to_peers`` + NODE_RELAY to an offline peer)."""
    dir_a = tmp_path_factory.mktemp("gfs-solo")
    a = await _start_node(dir_a, "A")
    try:
        cluster_a: ClusterService = a.app[gfs_cluster_key]
        # Manually register an unreachable peer row so `_broadcast`
        # iterates through it.
        from socialhome.global_server.domain import ClusterNode

        await a.app[gfs_cluster_repo_key].insert_node(
            ClusterNode(
                node_id="ghost",
                url="http://127.0.0.1:1",
                # A shared-seed sibling: only members get fan-out.
                public_key=a.app[gfs_cluster_key].own_public_key_hex,
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
                status="active",
            )
        )
    finally:
        await _stop_node(a)


async def _wait_for(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_heartbeat_loop_tracks_peer_liveness(
    tmp_path_factory, monkeypatch, fast_sync_retry
):
    """One heartbeat tick per ``HEARTBEAT_INTERVAL_S`` (30 s in production,
    shrunk here) must: ping every peer, mark a reachable one online and send
    it a NODE_HEARTBEAT; mark an unreachable one offline after
    ``HEARTBEAT_FAIL_THRESHOLD`` misses; and bring a reachable offline peer
    back online. The loop used to be covered only by accident — by a test
    slow enough to outlive the 30 s interval."""
    monkeypatch.setattr(cluster_mod, "HEARTBEAT_INTERVAL_S", 0.05)
    heartbeats: list[str] = []
    real_handle = ClusterService.handle_heartbeat

    async def _recording_handle(self, from_node_id, payload=None):
        heartbeats.append(from_node_id)
        await real_handle(self, from_node_id, payload)

    monkeypatch.setattr(ClusterService, "handle_heartbeat", _recording_handle)

    a = await _start_node(tmp_path_factory.mktemp("hb-a"), "A")
    b = await _start_node(tmp_path_factory.mktemp("hb-b"), "B")
    try:
        url_b = str(b.make_url("")).rstrip("/")
        await _approve(a, b, "B")
        await _approve(b, a, "A")
        repo_a = a.app[gfs_cluster_repo_key]
        await repo_a.insert_node(
            ClusterNode(
                node_id="ghost",
                url="http://127.0.0.1:1",
                # A shared-seed sibling: only members are pinged.
                public_key=a.app[gfs_cluster_key].own_public_key_hex,
                status="online",
            )
        )

        async def _status(node_id: str) -> str | None:
            for n in await repo_a.list_nodes():
                if n.node_id == node_id:
                    return n.status
            return None

        # Reachable peer: online, and it receives our NODE_HEARTBEAT.
        assert await _wait_for(lambda: _async_true("A" in heartbeats))
        assert await _status("B") == "online"
        # Unreachable peer: offline once the miss threshold is crossed.
        assert await _wait_for(
            lambda: _async_eq(_status("ghost"), "offline"),
        )
        # A reachable peer recorded offline is probed and comes back. Its own
        # row id ("B-alias") is one B never heartbeats as, so only the probe
        # can flip it back.
        await repo_a.insert_node(
            ClusterNode(
                node_id="B-alias",
                url=url_b,
                public_key=a.app[gfs_cluster_key].own_public_key_hex,
                status="offline",
            )
        )
        assert await _wait_for(lambda: _async_eq(_status("B-alias"), "online"))
    finally:
        await _stop_node(a)
        await _stop_node(b)


async def _async_true(value: bool) -> bool:
    return value


async def _async_eq(awaitable, expected) -> bool:
    return (await awaitable) == expected


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
            "status": "banned",
        },
    )
    await svc.apply_sync_client(
        "upsert",
        {
            "instance_id": "x",
            "public_key": "aa" * 32,
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


async def test_apply_sync_space_withdrawn_wins_lww(started_app):
    """Withdrawn-wins, mirroring ban-wins: a peer gossiping a stale
    ``withdrawn=0`` row must not silently re-list a space its owner
    delisted here."""
    svc: ClusterService = started_app[gfs_cluster_key]
    fed = started_app[gfs_fed_repo_key]
    await svc.apply_sync_client(
        "upsert",
        {
            "instance_id": "o",
            "public_key": "aa" * 32,
            "status": "active",
        },
    )
    await svc.apply_sync_space(
        "upsert",
        {
            "space_id": "wd",
            "owning_instance": "o",
            "status": "active",
            "withdrawn": True,
        },
    )
    await svc.apply_sync_space(
        "upsert",
        {
            "space_id": "wd",
            "owning_instance": "o",
            "status": "active",
            "withdrawn": False,
        },
    )
    assert (await fed.get_space("wd")).withdrawn is True


async def test_apply_sync_space_preserves_pin_icon_and_colour(started_app):
    """A cluster sync round-trip keeps the TOFU-pinned authority key, the
    icon and the primary colour — the wire shape used to drop all three,
    so any authenticated peer's NODE_SYNC_SPACE wiped the pin."""
    svc: ClusterService = started_app[gfs_cluster_key]
    fed = started_app[gfs_fed_repo_key]
    await svc.apply_sync_client(
        "upsert",
        {
            "instance_id": "o",
            "public_key": "aa" * 32,
            "status": "active",
        },
    )
    seeded = GlobalSpace(
        space_id="pin",
        owning_instance="o",
        name="Pinned",
        icon_url="https://cdn.example/icon.png",
        primary_color="#123456",
        status="active",
        identity_public_key="cc" * 32,
    )
    await fed.upsert_space(seeded)

    await svc.apply_sync_space("upsert", cluster_mod._space_to_wire(seeded))
    stored = await fed.get_space("pin")
    assert stored.identity_public_key == "cc" * 32
    assert stored.icon_url == "https://cdn.example/icon.png"
    assert stored.primary_color == "#123456"


async def test_apply_sync_report_with_bad_wire_is_silent(started_app):
    """Malformed wire shapes silently drop (``except (KeyError, ValueError)``)."""
    svc: ClusterService = started_app[gfs_cluster_key]
    # Missing the required 'id' key — must not raise.
    await svc.apply_sync_report({"target_type": "space"})


async def test_handle_heartbeat_updates_last_seen(started_app):
    """``handle_heartbeat`` refreshes a known peer's ``status`` + last_seen."""
    svc: ClusterService = started_app[gfs_cluster_key]
    from socialhome.global_server.domain import ClusterNode

    await started_app[gfs_cluster_repo_key].insert_node(
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
        status="active",
        auto_accept=True,
        connected_at="2026-01-01T00:00:00",
    )
    wire = cluster_mod._client_to_wire(c)
    assert _wire_to_client(wire) == c
    # The GFS keeps no household address: a replicated client row never
    # carries one, and one from a not-yet-upgraded node is ignored.
    assert "inbox_url" not in wire
    assert _wire_to_client({**wire, "inbox_url": "http://x/wh"}) == c

    s = GlobalSpace(
        space_id="s",
        owning_instance="o",
        name="N",
        description="D",
        about_markdown="M",
        cover_url="U",
        icon_url="I",
        min_age=13,
        category="gaming",
        # Both directory dials travel: without them a peer sync rebuilds the
        # row with the fail-closed defaults and silently makes a readable
        # space unreadable.
        join_mode="request",
        allow_subscribers=True,
        accent_color="#abcdef",
        primary_color="#123456",
        status="active",
        subscriber_count=3,
        posts_per_week=1.5,
        published_at="2026-01-01T00:00:00",
        identity_public_key="cc" * 32,
        authority_cert={"key_epoch": 2, "space_id": "s"},
        authority_rotation_seq=7,
        withdrawn=True,
    )
    # Every field round-trips. ``identity_public_key`` in particular: it used
    # to fall off the wire, so an inbound NODE_SYNC_SPACE rebuilt the row with
    # an empty pin and wiped the TOFU-pinned space authority key.
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


async def _cert_world(started_app):
    """A registered owner (derivable id) with a space pinned to K1."""
    svc: ClusterService = started_app[gfs_cluster_key]
    fed = started_app[gfs_fed_repo_key]
    owner = generate_identity_keypair()
    owner_id = derive_instance_id(owner.public_key)
    await svc.apply_sync_client(
        "upsert",
        {
            "instance_id": owner_id,
            "public_key": owner.public_key.hex(),
            "status": "active",
        },
    )
    k1 = generate_identity_keypair()
    seeded = GlobalSpace(
        space_id="rot",
        owning_instance=owner_id,
        name="R",
        status="active",
        identity_public_key=k1.public_key.hex(),
    )
    await fed.upsert_space(seeded)
    return svc, fed, owner, owner_id, seeded


def _cluster_cert(owner, owner_id, pk_hex, epoch):
    return sign_authority_cert(
        space_id="rot",
        owner_instance_id=owner_id,
        owner_seed=owner.private_key,
        owner_pk_hex=owner.public_key.hex(),
        authority_pk_hex=pk_hex,
        key_epoch=epoch,
    )


async def test_cluster_sync_repins_only_with_a_valid_owner_cert(started_app):
    """v_44: a peer node's NODE_SYNC_SPACE moves the pin only when it
    carries the owner's cert for the new key — never on its say-so."""
    svc, fed, owner, owner_id, seeded = await _cert_world(started_app)
    k2 = generate_identity_keypair().public_key.hex()
    # No cert: the pin stays.
    await svc.apply_sync_space(
        "upsert", cluster_mod._space_to_wire(replace(seeded, identity_public_key=k2))
    )
    assert (
        await fed.get_space("rot")
    ).identity_public_key == seeded.identity_public_key
    # Forged cert (another household's key): the pin stays.
    evil = generate_identity_keypair()
    forged = _cluster_cert(evil, owner_id, k2, 1)
    await svc.apply_sync_space(
        "upsert",
        cluster_mod._space_to_wire(
            replace(seeded, identity_public_key=k2, authority_cert=forged)
        ),
    )
    assert (
        await fed.get_space("rot")
    ).identity_public_key == seeded.identity_public_key
    # The owner's cert: re-pinned, cert stored.
    cert = _cluster_cert(owner, owner_id, k2, 1)
    await svc.apply_sync_space(
        "upsert",
        cluster_mod._space_to_wire(
            replace(seeded, identity_public_key=k2, authority_cert=cert)
        ),
    )
    row = await fed.get_space("rot")
    assert (row.identity_public_key, row.authority_cert) == (k2, cert)
    # A replay of the older state cannot move it back.
    await svc.apply_sync_space("upsert", cluster_mod._space_to_wire(seeded))
    assert (await fed.get_space("rot")).identity_public_key == k2


async def test_cluster_sync_new_row_drops_an_invalid_cert(started_app):
    svc: ClusterService = started_app[gfs_cluster_key]
    fed = started_app[gfs_fed_repo_key]
    owner = generate_identity_keypair()
    owner_id = derive_instance_id(owner.public_key)
    await svc.apply_sync_client(
        "upsert",
        {
            "instance_id": owner_id,
            "public_key": owner.public_key.hex(),
            "status": "active",
        },
    )
    k2 = generate_identity_keypair().public_key.hex()
    await svc.apply_sync_space(
        "upsert",
        cluster_mod._space_to_wire(
            GlobalSpace(
                space_id="rot",
                owning_instance=owner_id,
                name="R",
                status="active",
                identity_public_key=k2,
                authority_cert={"key_epoch": 4},
            )
        ),
    )
    row = await fed.get_space("rot")
    assert row.identity_public_key == k2
    assert row.authority_cert is None


async def test_cluster_sync_never_moves_the_rotation_seq_backwards(started_app):
    """F2: ``authority_rotation_seq`` travels in the cluster wire and is
    max-merged — a node that re-pins from the cert catches up with the
    publishing node's seq, and a stale gossip never lowers it. A seq for a
    pin this node does not hold is not taken."""
    svc, fed, owner, owner_id, seeded = await _cert_world(started_app)
    k2 = generate_identity_keypair().public_key.hex()
    cert = _cluster_cert(owner, owner_id, k2, 1)
    rotated = replace(
        seeded, identity_public_key=k2, authority_cert=cert, authority_rotation_seq=5
    )
    await svc.apply_sync_space("upsert", cluster_mod._space_to_wire(rotated))
    assert (await fed.get_space("rot")).authority_rotation_seq == 5
    # Stale gossip (same pin, lower seq): unchanged.
    await svc.apply_sync_space(
        "upsert", cluster_mod._space_to_wire(replace(rotated, authority_rotation_seq=2))
    )
    assert (await fed.get_space("rot")).authority_rotation_seq == 5
    # A seq for an uncertified pin: not taken.
    k3 = generate_identity_keypair().public_key.hex()
    await svc.apply_sync_space(
        "upsert",
        cluster_mod._space_to_wire(
            replace(
                rotated,
                identity_public_key=k3,
                authority_cert=None,
                authority_rotation_seq=99,
            )
        ),
    )
    row = await fed.get_space("rot")
    assert (row.identity_public_key, row.authority_rotation_seq) == (k2, 5)
    # Same pin, higher seq: caught up.
    await svc.apply_sync_space(
        "upsert", cluster_mod._space_to_wire(replace(rotated, authority_rotation_seq=8))
    )
    assert (await fed.get_space("rot")).authority_rotation_seq == 8
    # A malformed seq is ignored.
    wire = cluster_mod._space_to_wire(rotated)
    wire["authority_rotation_seq"] = "lots"
    await svc.apply_sync_space("upsert", wire)
    assert (await fed.get_space("rot")).authority_rotation_seq == 8


async def test_a_frame_for_one_node_is_refused_by_another(tmp_path_factory):
    """Cross-node replay: B's frame for A, captured and replayed to C (which
    also approved B), is refused — the signed ``to`` names A. Old-shape
    frames without ``to`` still reach both."""
    a = await _start_node(tmp_path_factory.mktemp("xr-a"), "A")
    b = await _start_node(tmp_path_factory.mktemp("xr-b"), "B")
    c = await _start_node(tmp_path_factory.mktemp("xr-c"), "C")
    try:
        for here in (a, c):
            await _approve(here, b, "B")
        cluster_b: ClusterService = b.app[gfs_cluster_key]
        raw, sig = cluster_b._signed_frame(NODE_HEARTBEAT, {}, to="A")
        headers = {"Content-Type": "application/json", "X-Node-Signature": sig}
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{str(c.make_url('')).rstrip('/')}/cluster/sync",
                data=raw,
                headers=headers,
            ) as resp:
                assert (resp.status, await resp.json()) == (
                    409,
                    {"error": "wrong_recipient"},
                )
            async with http.post(
                f"{str(a.make_url('')).rstrip('/')}/cluster/sync",
                data=raw,
                headers=headers,
            ) as resp:
                assert resp.status == 200
    finally:
        for node in (a, b, c):
            await _stop_node(node)


async def test_fan_out_with_recipients_converges_across_three_nodes(
    tmp_path_factory, fast_sync_retry
):
    """Every node approves every other; a policy push from A, sent with a
    per-recipient ``to``, lands on both B and C."""
    nodes = {
        nid: await _start_node(tmp_path_factory.mktemp(f"fan-{nid}"), nid)
        for nid in ("A", "B", "C")
    }
    try:
        for here_id, here in nodes.items():
            for there_id, there in nodes.items():
                if here_id != there_id:
                    await _approve(here, there, there_id)
        await nodes["A"].app[gfs_cluster_key].sync_policy({"fraud_threshold": 7})
        for nid in ("B", "C"):
            assert (
                await nodes[nid].app[gfs_admin_repo_key].get_config("fraud_threshold")
                == "7"
            )
    finally:
        for node in nodes.values():
            await _stop_node(node)


# ─── Owner notices + targeted relays across two real nodes on ONE DB ──────
#
# The production topology: N GFS processes share one SQLite file and one
# identity seed behind a round-robin balancer, so a household's socket and
# its next HTTP request usually land on DIFFERENT nodes. Before the fix a
# ``new_subscriber`` notice reached the owner only if the subscribe hit the
# node holding the owner's socket, and a targeted ``to`` relay only if the
# publish hit the node holding the target's socket.

_OWNER_ID = "ownerhome222222222222222222222aa"
_FOLLOWER_ID = "followerhome2222222222222222222a"
_SPACE_ID = "sp-cluster-notice"


def _shared_config(data_dir, node_id: str) -> GfsConfig:
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url=_UNREACHABLE_SELF_URL,
        data_dir=str(data_dir),
        instance_id="gfs-shared",
        cluster_enabled=True,
        cluster_node_id=node_id,
        cluster_peers=(),
    )


async def _ws_hello(ws, instance_id: str, seed: bytes) -> None:
    ts = int(time.time())
    await ws.send_json(
        {
            "type": "hello",
            "instance_id": instance_id,
            "ts": ts,
            "sig": b64url_encode(sign_ed25519(seed, f"{instance_id}|{ts}".encode())),
        }
    )


async def _await_connected(app, instance_id: str) -> None:
    registry = app[gfs_ws_registry_key]
    assert await _wait_for(lambda: _async_true(registry.is_connected(instance_id)))


async def _next_frame(ws, frame_type: str, *, timeout: float = 5.0) -> dict:
    """The next frame of *frame_type* (others — e.g. a reconnect notice — are
    skipped)."""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, f"no {frame_type} frame within {timeout}s"
        frame = await asyncio.wait_for(ws.receive_json(), timeout=left)
        if frame.get("type") == frame_type:
            return frame


@pytest.fixture
async def shared_db_nodes(tmp_path_factory, monkeypatch):
    """Two cluster nodes A and B on one data dir (one DB, one seed)."""
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_DELAY_S", 0.01)
    shared = tmp_path_factory.mktemp("gfs-shared")
    a = TestServer(create_gfs_app(_shared_config(shared, "node-a")))
    await a.start_server()
    # B attaches to the DB A already migrated.
    b = TestServer(create_gfs_app(_shared_config(shared, "node-b")))
    await b.start_server()
    own_key = a.app[gfs_cluster_key].own_public_key_hex
    assert b.app[gfs_cluster_key].own_public_key_hex == own_key
    repo = a.app[gfs_cluster_repo_key]
    for node_id, server in (("node-a", a), ("node-b", b)):
        # Shared-seed siblings: members by our own key, no approval needed.
        await repo.insert_node(
            ClusterNode(
                node_id=node_id,
                url=str(server.make_url("")).rstrip("/"),
                public_key=own_key,
                status="online",
            )
        )
    try:
        yield a, b
    finally:
        await b.close()
        await a.close()


async def _seed_households(app) -> tuple[bytes, bytes, bytes]:
    """Owner + follower registered, the owner's space published with
    followers on. Returns (owner seed, follower seed, space seed)."""
    owner = generate_identity_keypair()
    follower = generate_identity_keypair()
    space = generate_identity_keypair()
    fed_repo = app[gfs_fed_repo_key]
    for iid, kp in ((_OWNER_ID, owner), (_FOLLOWER_ID, follower)):
        await fed_repo.upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key=kp.public_key.hex(),
                status="active",
                keywrap_public_key="cc" * 32,
                kem_suite="x25519",
                keywrap_sig="a2V5d3JhcHNpZw",
            )
        )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id=_SPACE_ID,
            owning_instance=_OWNER_ID,
            name="Cluster",
            status="active",
            allow_subscribers=True,
            identity_public_key=space.public_key.hex(),
        )
    )
    return owner.private_key, follower.private_key, space.private_key


def _signed_subscribe_body(seed: bytes) -> dict:
    ts = datetime.now(UTC).isoformat()
    signed = {
        "action": "subscribe",
        "instance_id": _FOLLOWER_ID,
        "space_id": _SPACE_ID,
        "ts": ts,
    }
    canonical = json.dumps(signed, separators=(",", ":"), sort_keys=True).encode()
    return {
        **signed,
        "signature": b64url_encode(sign_ed25519(seed, canonical)),
        "accepts": ["author_profiles_bundle"],
    }


async def test_a_subscribe_on_one_node_notifies_the_owner_on_another(
    shared_db_nodes,
):
    a, b = shared_db_nodes
    owner_seed, follower_seed, _space_seed = await _seed_households(a.app)
    async with (
        aiohttp.ClientSession() as http,
        http.ws_connect(a.make_url("/gfs/ws")) as owner_ws,
    ):
        await _ws_hello(owner_ws, _OWNER_ID, owner_seed)
        await _await_connected(a.app, _OWNER_ID)
        # The balancer sent the follower's subscribe to node B.
        async with http.post(
            b.make_url("/gfs/subscribe"), json=_signed_subscribe_body(follower_seed)
        ) as resp:
            assert resp.status == 200
        frame = await _next_frame(owner_ws, "new_subscriber")
    assert frame["reason"] == "subscribe"
    assert frame["space_id"] == _SPACE_ID
    assert frame["subscriber"]["instance_id"] == _FOLLOWER_ID
    assert frame["subscriber"]["keywrap_public_key"] == "cc" * 32
    assert frame["subscriber"]["accepts"] == ["author_profiles_bundle"]


async def test_a_targeted_relay_on_one_node_reaches_a_socket_on_another(
    shared_db_nodes,
):
    a, b = shared_db_nodes
    _owner_seed, follower_seed, space_seed = await _seed_households(a.app)
    await a.app[gfs_fed_repo_key].add_subscriber(
        space_id=_SPACE_ID, instance_id=_FOLLOWER_ID
    )
    payload: dict = {"space_id": _SPACE_ID, "sealed": {"ciphertext": "for-you"}}
    payload.update(
        sign_authority_event(
            event_type="space_subscriber_key_handoff",
            space_id=_SPACE_ID,
            payload=payload,
            space_seed=space_seed,
        )
    )
    async with (
        aiohttp.ClientSession() as http,
        http.ws_connect(b.make_url("/gfs/ws")) as follower_ws,
    ):
        await _ws_hello(follower_ws, _FOLLOWER_ID, follower_seed)
        await _await_connected(b.app, _FOLLOWER_ID)
        # The owner's targeted handoff landed on node A.
        async with http.post(
            a.make_url("/gfs/publish"),
            json={
                "space_id": _SPACE_ID,
                "event_type": "space_subscriber_key_handoff",
                "payload": payload,
                "to": _FOLLOWER_ID,
            },
        ) as resp:
            assert resp.status == 200
            assert (await resp.json())["delivered_to"] == 1
        frame = await _next_frame(follower_ws, "relay")
    assert frame == {
        "type": "relay",
        "space_id": _SPACE_ID,
        "event_type": "space_subscriber_key_handoff",
        "payload": payload,
    }
    queue = a.app[gfs_envelope_queue_repo_key]
    assert await _wait_for(lambda: _async_eq(queue.count_for(_FOLLOWER_ID), 0)), (
        "the drained row must leave the shared queue"
    )


# ─── Broadcast fan-out across THREE real nodes on ONE DB ──────────────────
#
# Before the fix every broadcast item — an authority-signed ``/gfs/publish``,
# a member publish, a channel item — reached only the sockets of the node
# that took the request: on N nodes behind a round-robin balancer (N−1)/N of
# the followers missed each post.

_FOLLOWER_IDS = (
    "followerb2222222222222222222222b",
    "followerc2222222222222222222222c",
    "followera2222222222222222222222a",
)
_OFFLINE_ID = "offlinehome22222222222222222222o"
_PUBLISHER_ID = "publisherhome2222222222222222222"


@pytest.fixture
async def three_shared_nodes(tmp_path_factory, monkeypatch):
    """Three cluster nodes A, B, C on one data dir (one DB, one seed)."""
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_DELAY_S", 0.01)
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_MIN_GAP_S", 0.0, raising=False)
    shared = tmp_path_factory.mktemp("gfs-three")
    names = ("node-a", "node-b", "node-c")
    nodes = []
    for node_id in names:
        server = TestServer(create_gfs_app(_shared_config(shared, node_id)))
        await server.start_server()
        nodes.append(server)
    own_key = nodes[0].app[gfs_cluster_key].own_public_key_hex
    repo = nodes[0].app[gfs_cluster_repo_key]
    for node_id, server in zip(names, nodes, strict=True):
        # Shared-seed siblings: members by our own key, no approval needed.
        await repo.insert_node(
            ClusterNode(
                node_id=node_id,
                url=str(server.make_url("")).rstrip("/"),
                public_key=own_key,
                status="online",
            )
        )
    try:
        yield nodes
    finally:
        for server in reversed(nodes):
            await server.close()


async def _seed_space_with_followers(app, ids) -> tuple[bytes, dict[str, bytes]]:
    """A published space with *ids* subscribed. Returns (space seed,
    instance id → identity seed)."""
    space = generate_identity_keypair()
    fed_repo = app[gfs_fed_repo_key]
    seeds: dict[str, bytes] = {}
    for iid in ids:
        kp = generate_identity_keypair()
        seeds[iid] = kp.private_key
        await fed_repo.upsert_instance(
            ClientInstance(
                instance_id=iid,
                display_name=iid,
                public_key=kp.public_key.hex(),
                status="active",
            )
        )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id=_SPACE_ID,
            owning_instance=ids[0],
            name="Cluster",
            status="active",
            allow_subscribers=True,
            identity_public_key=space.public_key.hex(),
        )
    )
    for iid in ids:
        await fed_repo.add_subscriber(space_id=_SPACE_ID, instance_id=iid)
    return space.private_key, seeds


async def _relays_within(ws, seconds: float) -> list[dict]:
    """Every ``relay`` frame *ws* receives within *seconds*."""
    frames: list[dict] = []
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        try:
            msg = await ws.receive(timeout=left)
        except TimeoutError:
            break
        if msg.type != aiohttp.WSMsgType.TEXT:
            break
        frame = json.loads(msg.data)
        if frame.get("type") == "relay":
            frames.append(frame)
    return frames


async def _connect_all(http, placement, seeds) -> dict:
    """Open each household's socket on its node; ``{instance id: ws}``."""
    sockets = {}
    for iid, node in placement:
        ws = await http.ws_connect(node.make_url("/gfs/ws"))
        await _ws_hello(ws, iid, seeds[iid])
        await _await_connected(node.app, iid)
        sockets[iid] = ws
    return sockets


def _signed_post(space_seed: bytes, text: str) -> dict:
    payload: dict = {"space_id": _SPACE_ID, "epoch": 1, "ciphertext": text}
    payload.update(
        sign_authority_event(
            event_type="space_post_public",
            space_id=_SPACE_ID,
            payload=payload,
            space_seed=space_seed,
        )
    )
    return payload


async def test_a_public_post_on_one_node_reaches_followers_on_every_node_once(
    three_shared_nodes,
):
    a, b, c = three_shared_nodes
    on_b, on_c, on_a = _FOLLOWER_IDS
    space_seed, seeds = await _seed_space_with_followers(a.app, _FOLLOWER_IDS)
    payload = _signed_post(space_seed, "hello cluster")
    async with aiohttp.ClientSession() as http:
        sockets = await _connect_all(http, ((on_a, a), (on_b, b), (on_c, c)), seeds)
        # The balancer sent the publish to node A.
        async with http.post(
            a.make_url("/gfs/publish"),
            json={
                "space_id": _SPACE_ID,
                "event_type": "space_post_public",
                "payload": payload,
            },
        ) as resp:
            assert resp.status == 200
        got = {iid: await _relays_within(ws, 1.5) for iid, ws in sockets.items()}
        for ws in sockets.values():
            await ws.close()
    want = {
        "type": "relay",
        "space_id": _SPACE_ID,
        "event_type": "space_post_public",
        "payload": payload,
    }
    assert got == {on_a: [want], on_b: [want], on_c: [want]}, (
        "each follower — on the publishing node and on both others — gets the "
        "post exactly once"
    )
    # A broadcast is WebSocket-only: nothing was queued for anybody.
    queue = a.app[gfs_envelope_queue_repo_key]
    for iid in _FOLLOWER_IDS:
        assert await queue.count_for(iid) == 0


async def test_a_replayed_post_on_another_node_is_not_fanned_out_again(
    three_shared_nodes,
):
    """The forwarded item is remembered on every node, so the same bytes
    re-POSTed to node B inside the replay window go nowhere — before, each
    node had to see the bytes once itself."""
    a, b, _c = three_shared_nodes
    on_b = _FOLLOWER_IDS[0]
    space_seed, seeds = await _seed_space_with_followers(a.app, _FOLLOWER_IDS)
    body = {
        "space_id": _SPACE_ID,
        "event_type": "space_post_public",
        "payload": _signed_post(space_seed, "once"),
    }
    async with aiohttp.ClientSession() as http:
        (ws,) = (await _connect_all(http, ((on_b, b),), seeds)).values()
        async with http.post(a.make_url("/gfs/publish"), json=body) as resp:
            assert resp.status == 200
        first = await _relays_within(ws, 1.0)
        async with http.post(b.make_url("/gfs/publish"), json=body) as resp:
            assert resp.status == 200
            assert (await resp.json())["delivered_to"] == 0
        again = await _relays_within(ws, 1.0)
        await ws.close()
    assert len(first) == 1
    assert again == []


async def test_a_member_item_reaches_every_node_and_queues_only_the_offline(
    three_shared_nodes, monkeypatch
):
    """A member item accepted by node A: followers on B and C get it live
    exactly once — including one whose socket is too new to count as
    "seen" — the publisher does not, and the ONE recently-seen follower with
    no socket anywhere gets the only queue row. Before, a follower on another
    node got the item only through a queue row, and only if it had been
    seen: the one on C got nothing, and B's copy cost a row."""
    a, b, c = three_shared_nodes
    on_b, on_c, on_a = _FOLLOWER_IDS
    ids = (*_FOLLOWER_IDS, _OFFLINE_ID, _PUBLISHER_ID)
    _space_seed, seeds = await _seed_space_with_followers(a.app, ids)
    fed = a.app[gfs_fed_repo_key]
    now = int(time.time())
    for iid in (on_a, on_b, _OFFLINE_ID, _PUBLISHER_ID):
        await fed.mark_relay_seen(iid, at=now)
    # ``on_c`` just connected: never marked seen.
    queue = a.app[gfs_envelope_queue_repo_key]
    # Every relay row any of the three nodes writes.
    rows_for: list[str] = []
    real_enqueue = type(queue).enqueue_relay

    async def _recording_enqueue(self, target, *args, **kwargs):
        rows_for.append(target)
        return await real_enqueue(self, target, *args, **kwargs)

    monkeypatch.setattr(type(queue), "enqueue_relay", _recording_enqueue)
    frame = {
        "space_id": _SPACE_ID,
        "event_type": "space_item",
        "epoch": 1,
        "payload": "c2VhbGVk",
    }
    member_publish = a.app[gfs_member_publish_key]
    async with aiohttp.ClientSession() as http:
        sockets = await _connect_all(
            http,
            ((on_a, a), (on_b, b), (on_c, c), (_PUBLISHER_ID, c)),
            seeds,
        )
        assert member_publish._submit(
            mp_mod._FanOutJob(
                space_id=_SPACE_ID, publisher_id=_PUBLISHER_ID, frame=frame
            )
        )
        await member_publish.wait_idle()
        got = {iid: await _relays_within(ws, 1.5) for iid, ws in sockets.items()}
        for ws in sockets.values():
            await ws.close()
    want = {"type": "relay", **frame}
    assert got == {on_a: [want], on_b: [want], on_c: [want], _PUBLISHER_ID: []}
    assert await _wait_for(lambda: _async_eq(queue.count_for(_OFFLINE_ID), 1))
    assert rows_for == [_OFFLINE_ID], "one row, for the household no node holds"


async def test_a_channel_item_reaches_seats_on_every_node_once(three_shared_nodes):
    """A private-channel item accepted by node A reaches the seats whose
    sockets are on B and C — each node resolves the channel's open-epoch
    seats from the shared DB."""
    a, b, c = three_shared_nodes
    on_b, on_c, on_a = _FOLLOWER_IDS
    _space_seed, seeds = await _seed_space_with_followers(a.app, _FOLLOWER_IDS)
    channel_id = new_channel_id()
    channels = a.app[gfs_channel_repo_key]
    now = int(time.time())
    assert await channels.register(
        channel_id, channel_suite="ed25519", channel_pk="aa" * 32, now=now
    )
    assert await channels.set_epoch(channel_id, 3, expected=None, now=now)
    for iid in _FOLLOWER_IDS:
        await channels.add_subscriber(channel_id, iid, pass_epoch=3, now=now)
    frame = channel_frame(channel_id, 3, "c2VhbGVk")
    async with aiohttp.ClientSession() as http:
        sockets = await _connect_all(http, ((on_a, a), (on_b, b), (on_c, c)), seeds)
        a.app[gfs_channel_service_key]._submit(channel_id, "", frame)
        await a.app[gfs_member_publish_key].wait_idle()
        got = {iid: await _relays_within(ws, 1.5) for iid, ws in sockets.items()}
        for ws in sockets.values():
            await ws.close()
    want = {"type": "relay", **frame}
    assert got == {on_a: [want], on_b: [want], on_c: [want]}

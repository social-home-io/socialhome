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
from dataclasses import replace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.authority_cert import sign_authority_cert
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.global_server import cluster as cluster_mod
from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_cluster_key,
    gfs_cluster_repo_key,
    gfs_fed_repo_key,
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

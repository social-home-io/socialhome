"""GFS mailboxes — review round 1 hardening (#1326): instance takeover,
queue pools and caps, day-granular timestamps, held sockets, per-address
socket caps across prefixes, no mailbox id in any log, GFS id aliases."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import replace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.global_server import cluster as cluster_mod
from socialhome.global_server.app_keys import (
    gfs_cluster_key,
    gfs_envelope_queue_repo_key,
    gfs_envelope_relay_key,
    gfs_fed_repo_key,
    gfs_mailbox_repo_key,
    gfs_mailbox_service_key,
    gfs_token_service_key,
)
from socialhome.global_server.config import EnvelopeMailboxConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.mailboxes import (
    DAY_SECONDS,
    CountMinSketch,
    GfsMailboxService,
    address_scopes,
)
from socialhome.global_server.repositories import (
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsFederationRepo,
    SqliteGfsMailboxRepo,
)
from socialhome.global_server.routes import mailbox as mailbox_routes
from socialhome.global_server.server import create_gfs_app
from tests.global_server.test_mailbox_cluster import _config, _url
from tests.global_server.test_mailboxes import (
    GFS_ID,
    SEALED,
    Box,
    FreeTickets,
    _cfg,
    _claim,
    _register,
    _settle,
    ticketed,
)


async def _client(tmp_dir, **cfg) -> TestClient:
    app = create_gfs_app(_cfg(tmp_dir, **cfg))
    tc = TestClient(TestServer(app))
    await tc.start_server()
    tc._app = app
    return tc


@pytest.fixture
async def gfs(tmp_dir):
    tc = await _client(tmp_dir)
    try:
        yield tc
    finally:
        await tc.close()


async def _token(tc) -> str:
    token, _wait = await tc._app[gfs_token_service_key].generate(
        "test-" + secrets.token_hex(4)
    )
    assert token
    return token


async def _register_instance(tc, instance_id: str, public_key: str):
    return await tc.post(
        "/gfs/register",
        json={
            "token": await _token(tc),
            "instance_id": instance_id,
            "public_key": public_key,
        },
    )


# ── 1. Instance registration can't take a mailbox (or a household) over ──


@pytest.mark.security
async def test_a_mailbox_id_cannot_be_registered_as_an_instance(gfs):
    box = Box()
    assert (await _register(gfs, box)).status == 200
    attacker_pk = ed25519_public_key(b"\x09" * 32).hex()
    resp = await _register_instance(gfs, box.id, attacker_pk)
    assert resp.status == 400
    assert (await resp.json())["error"] == "instance_id_mismatch"
    assert await gfs._app[gfs_fed_repo_key].get_instance(box.id) is None
    # The mailbox still gets its traffic.
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    await _settle(gfs._app)
    assert await gfs._app[gfs_envelope_queue_repo_key].count_for(box.id) == 1


@pytest.mark.security
async def test_an_instance_id_must_be_derived_from_its_key(gfs):
    pk = ed25519_public_key(b"\x0a" * 32)
    other = ed25519_public_key(b"\x0b" * 32)
    resp = await _register_instance(gfs, derive_instance_id(other), pk.hex())
    assert resp.status == 400
    assert (await resp.json())["error"] == "instance_id_mismatch"
    ok = await _register_instance(gfs, derive_instance_id(pk), pk.hex())
    assert ok.status == 200


@pytest.mark.security
async def test_a_different_key_never_takes_a_registered_row_over(gfs):
    """A grandfathered (non-derived) row keeps its key: a re-register with
    the same key updates it, any other key is refused."""
    legacy_id = "legacy2home2222222222222222222aa"
    pk_a = ed25519_public_key(b"\x0c" * 32).hex()
    pk_b = ed25519_public_key(b"\x0d" * 32).hex()
    await gfs._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(instance_id=legacy_id, display_name="old", public_key=pk_a)
    )
    same = await _register_instance(gfs, legacy_id, pk_a)
    assert same.status == 200
    other = await _register_instance(gfs, legacy_id, pk_b)
    assert other.status == 400
    row = await gfs._app[gfs_fed_repo_key].get_instance(legacy_id)
    assert row is not None and row.public_key == pk_a


async def test_pinned_registration_refuses_a_key_change_and_a_mailbox_id(gfs_db):
    fed = SqliteGfsFederationRepo(gfs_db)
    boxes = SqliteGfsMailboxRepo(gfs_db)
    pk_a = "aa" * 32
    row = ClientInstance(instance_id="i" * 32, display_name="x", public_key=pk_a)
    assert await fed.register_instance_pinned(row) is True
    swapped = ClientInstance(
        instance_id="i" * 32, display_name="y", public_key="bb" * 32
    )
    assert await fed.register_instance_pinned(swapped) is False
    assert (await fed.get_instance("i" * 32)).public_key == pk_a
    # Both directions of the collision.
    assert await boxes.register("m" * 32, b"\x01" * 32, now=0) == "created"
    clash = ClientInstance(instance_id="m" * 32, display_name="z", public_key=pk_a)
    assert await fed.register_instance_pinned(clash) is False
    assert await fed.get_instance("m" * 32) is None
    assert await boxes.register("i" * 32, b"\x02" * 32, now=0) == "collision"
    assert await boxes.get("i" * 32) is None


@pytest.mark.security
async def test_an_id_in_both_tables_gets_nothing_and_the_warning_names_no_id(
    gfs, caplog
):
    box = Box()
    await _register(gfs, box)
    # Written around the checks (an old node, a manual edit): both rows.
    await gfs._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=box.id,
            display_name="x",
            public_key="aa" * 32,
            status="active",
        )
    )
    caplog.set_level(logging.DEBUG)
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    await _settle(gfs._app)
    assert await gfs._app[gfs_envelope_queue_repo_key].count_for(box.id) == 0
    assert any("both as an instance" in r.getMessage() for r in caplog.records)
    assert all(box.id not in r.getMessage() for r in caplog.records)


# ── 2. Queue pools and caps ─────────────────────────────────────────────


async def _queue(gfs_db, *ids: str) -> SqliteGfsEnvelopeQueueRepo:
    boxes = SqliteGfsMailboxRepo(gfs_db)
    for mid in ids:
        await boxes.register(mid, mid.encode()[:32].ljust(32, b"x"), now=0)
    return SqliteGfsEnvelopeQueueRepo(gfs_db)


def _blob(n: int) -> str:
    return "x" * n


async def _put(q, to, n, *, mailbox, total=1000, at=10):
    return await q.enqueue(
        to,
        _blob(n),
        created_at=at,
        expires_at=10**10,
        max_per_recipient=100,
        max_bytes_per_recipient=10**6,
        max_total_bytes=total,
        mailbox_pool=mailbox,
    )


async def test_the_mailbox_pool_never_evicts_household_envelopes(gfs_db):
    a, b = "a" * 32, "b" * 32
    q = await _queue(gfs_db, a, b)
    household = "h" * 32
    row = 70 * 1024
    total = 4 * row
    assert await _put(q, household, 900, mailbox=False)
    for _ in range(4):
        assert await _put(q, a, row, mailbox=True, total=total)
    # The mailbox pool is full: b's envelope is admitted only within its
    # reserve (no ceiling given → none), never by removing anyone's rows.
    assert not await _put(q, b, row, mailbox=True, total=total)
    assert await q.count_for(a) == 4
    assert await q.count_for(household) == 1


async def test_the_household_pool_has_its_own_total_and_tail_drops(gfs_db):
    a = "a" * 32
    q = await _queue(gfs_db, a)
    assert await _put(q, a, 900, mailbox=True)
    h1, h2 = "h" * 32, "k" * 32
    assert await _put(q, h1, 900, mailbox=False)
    # The household pool is full on its own: the NEW envelope is dropped.
    assert not await _put(q, h2, 500, mailbox=False)
    assert await q.count_for(h1) == 1
    assert await q.count_for(a) == 1


async def test_mailbox_media_has_its_own_total(gfs_db):
    a = "a" * 32
    q = await _queue(gfs_db, a)
    kw = {"created_at": 1, "expires_at": 10**10, "max_bytes_per_recipient": 10**6}
    assert await q.enqueue_media("h" * 32, b"x" * 900, max_total_bytes=1000, **kw)
    assert await q.enqueue_media(
        a, b"x" * 900, max_total_bytes=1000, mailbox_pool=True, **kw
    )
    assert not await q.enqueue_media(
        a, b"x" * 200, max_total_bytes=1000, mailbox_pool=True, **kw
    )


async def test_a_mailbox_gets_its_own_small_per_recipient_caps(tmp_dir):
    tc = await _client(
        tmp_dir,
        envelope_mailbox=EnvelopeMailboxConfig(mailbox_queue_max_per_recipient=2),
    )
    try:
        box = Box()
        await _register(tc, box)
        for _ in range(4):
            await tc.post(
                "/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED}
            )
        await _settle(tc._app)
        assert await tc._app[gfs_envelope_queue_repo_key].count_for(box.id) == 2
    finally:
        await tc.close()


async def test_a_new_mailbox_needs_a_ticket_an_existing_one_does_not(tmp_dir):
    """The server-wide daily cap is gone: a NEW mailbox spends a ticket,
    re-registering an existing one is free."""
    tc = await _client(tmp_dir)
    try:
        first = Box()
        assert (await _register(tc, first)).status == 200
        bare = await tc.post("/gfs/mailbox/register", json=Box().register_body())
        assert bare.status == 403
        assert (await bare.json())["error"] == "ticket_required"
        again = await tc.post("/gfs/mailbox/register", json=first.register_body())
        assert again.status == 200  # existing: free
    finally:
        await tc.close()


# ── 3 + 4. Day-granular stamps; a held socket is never swept ───────────


async def test_stamps_are_day_granular(gfs):
    box = Box()
    await _register(gfs, box)
    ws = await _claim(gfs, box)
    await ws.receive_json(timeout=2)
    stored = await gfs._app[gfs_mailbox_repo_key].get(box.id)
    assert stored.created_at % DAY_SECONDS == 0
    assert stored.last_claim_at % DAY_SECONDS == 0
    await ws.close()


async def test_a_held_socket_refreshes_its_claim_and_is_not_swept(gfs, monkeypatch):
    monkeypatch.setattr(mailbox_routes, "MAILBOX_CLAIM_REFRESH_S", 0.02)
    svc: GfsMailboxService = gfs._app[gfs_mailbox_service_key]
    box = Box()
    await _register(gfs, box)
    ws = await _claim(gfs, box)
    await ws.receive_json(timeout=2)
    start = time.time()
    later = start + 40 * DAY_SECONDS
    monkeypatch.setattr(svc, "_clock", lambda: later)
    repo = gfs._app[gfs_mailbox_repo_key]
    for _ in range(200):
        stored = await repo.get(box.id)
        if stored.last_claim_at >= later - DAY_SECONDS:
            break
        await asyncio.sleep(0.02)
    assert stored.last_claim_at == int(later) // DAY_SECONDS * DAY_SECONDS
    assert await svc.sweep(now=int(later)) == 0
    assert await repo.get(box.id) is not None
    await ws.close()


# ── 5. Per-address caps: sockets, prefixes, no reset by eviction ───────


def test_an_ipv6_bucket_counts_against_its_56_and_48():
    scopes = address_scopes("2001:db8:1:2::/64")
    assert scopes == [
        ("2001:db8:1:2::/64", 1),
        ("2001:db8:1::/56", 16),
        ("2001:db8:1::/48", 256),
    ]
    assert address_scopes("192.0.2.7") == [("192.0.2.7", 1)]
    assert address_scopes("unknown") == [("unknown", 1)]


def _svc(**cfg) -> GfsMailboxService:
    return GfsMailboxService(
        repo=None,  # type: ignore[arg-type]
        gfs_instance_id=GFS_ID,
        config=EnvelopeMailboxConfig(**cfg),
        tickets=FreeTickets(),  # type: ignore[arg-type]
    )


def test_registrations_from_many_64s_hit_the_56_cap():
    svc = _svc(mailbox_max_per_ip=1)
    ok = [svc._count_new(f"2001:db8:1:{i:x}::/64", 1.0) for i in range(18)]
    assert [bool(x) for x in ok] == [True] * 16 + [False, False]


def test_flooding_other_buckets_cannot_reset_a_count():
    svc = _svc(mailbox_max_per_ip=2)
    assert svc._count_new("198.51.100.1", 1.0)
    assert svc._count_new("198.51.100.1", 1.0)
    for n in range(50_000):
        svc._count_new(f"10.{n >> 16 & 255}.{n >> 8 & 255}.{n & 255}", 1.0)
    assert not svc._count_new("198.51.100.1", 1.0)


def test_the_sketch_never_under_counts():
    sketch = CountMinSketch()
    for _ in range(3):
        sketch.add("k")
    assert sketch.estimate("k") >= 3
    sketch.add("k", -3)
    assert sketch.estimate("k") == 0


def test_socket_slots_are_counted_and_released():
    svc = _svc(mailbox_max_sockets_per_ip=1)
    slot = svc.acquire_socket("192.0.2.1")
    assert slot is not None
    assert svc.acquire_socket("192.0.2.1") is None
    svc.release_socket(slot)
    assert svc.acquire_socket("192.0.2.1") is not None


async def test_a_second_claim_socket_from_one_address_is_429(tmp_dir):
    tc = await _client(
        tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(mailbox_max_sockets_per_ip=1)
    )
    try:
        first = await tc.ws_connect("/gfs/mailbox/ws")
        await first.receive_json(timeout=2)
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await tc.ws_connect("/gfs/mailbox/ws")
        assert err.value.status == 429
        await first.close()
        await asyncio.sleep(0.05)
        again = await tc.ws_connect("/gfs/mailbox/ws")
        await again.close()
    finally:
        await tc.close()


# ── 6. No mailbox id in any log line, on any path ──────────────────────


@pytest.mark.security
async def test_no_mailbox_id_in_the_backlog_drain_or_hint_failure_logs(
    gfs, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    box = Box()
    await _register(gfs, box)
    relay = gfs._app[gfs_envelope_relay_key]
    # Backlog cap.
    monkeypatch.setattr(relay, "_max_inflight_per_recipient", 0)
    relay.submit(box.id, SEALED)
    monkeypatch.undo()
    # Drain lookup failure for an offline mailbox.

    async def _boom(*_a, **_k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(SqliteGfsEnvelopeQueueRepo, "list_for", _boom)
    await relay.drain(box.id)
    monkeypatch.undo()
    # Cluster hint drain failure.
    cluster = gfs._app[gfs_cluster_key]

    async def _failing_drain(_id):
        raise RuntimeError("drain failed")

    await cluster._run_hinted_drain(_failing_drain, box.id)
    messages = [r.getMessage() for r in caplog.records]
    assert any("backlog cap" in m for m in messages)
    assert any("drain lookup failed" in m for m in messages)
    assert any("hint drain failed" in m for m in messages)
    assert all(box.id not in m for m in messages)


# ── 7. GFS id aliases and per-node ids ─────────────────────────────────


async def test_a_registration_and_claim_bound_to_an_alias_are_accepted(tmp_dir):
    tc = await _client(tmp_dir, instance_id_aliases=("gfs-old",))
    try:
        box = Box()
        assert (await _register(tc, box, gfs_id="gfs-old")).status == 200
        ws = await tc.ws_connect("/gfs/mailbox/ws")
        hello = await ws.receive_json(timeout=2)
        assert hello["gfs_instance_id"] == GFS_ID
        await ws.send_json(box.claim(hello["challenge"], gfs_id="gfs-old"))
        assert (await ws.receive_json(timeout=2))["type"] == "mailbox_claimed"
        await ws.close()
    finally:
        await tc.close()


async def test_a_claim_bound_to_an_unknown_gfs_id_fails_like_a_wrong_key(gfs):
    box = Box()
    await _register(gfs, box)
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    hello = await ws.receive_json(timeout=2)
    await ws.send_json(box.claim(hello["challenge"], gfs_id="gfs-elsewhere"))
    closed = await ws.receive(timeout=2)
    assert (ws.close_code, closed.extra) == (4401, "auth-failed")


async def test_ids_served_by_shared_key_siblings_are_accepted(gfs_db):
    svc = GfsMailboxService(
        repo=SqliteGfsMailboxRepo(gfs_db),
        gfs_instance_id="gfs-1",
        config=EnvelopeMailboxConfig(),
        tickets=FreeTickets(),  # type: ignore[arg-type]
        aliases=("gfs-legacy",),
        sibling_ids=lambda: {"gfs-0", ""},
    )
    assert await svc.accepted_gfs_ids() == {"gfs-1", "gfs-legacy", "gfs-0"}


async def test_nodes_with_different_ids_register_on_one_claim_on_the_other(
    tmp_path_factory, monkeypatch
):
    """The production shape of a cluster still on per-alloc ids: register
    signed for node A's id, claim on node B (another id, same key + DB)."""
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_DELAY_S", 0.01)
    shared = tmp_path_factory.mktemp("gfs-two-ids")
    a = TestServer(create_gfs_app(_config(shared, "node-a")))
    await a.start_server()
    cfg_b = _config(shared, "node-b")
    b_app = create_gfs_app(replace(cfg_b, instance_id="gfs-other"))
    b = TestServer(b_app)
    await b.start_server()
    try:
        own = b.app[gfs_cluster_key].own_public_key_hex
        # Node B verified a frame from A under our shared key: A serves
        # ``gfs-shared`` (what a HELLO / heartbeat records).
        b.app[gfs_cluster_key]._note_sibling_identity("node-a", own, "gfs-shared")
        box = Box()
        async with aiohttp.ClientSession() as http:
            async with http.post(
                _url(a) + "/gfs/mailbox/register",
                json=await ticketed(b_app, box.register_body(gfs_id="gfs-shared")),
            ) as r:
                assert r.status == 200
            ws = await http.ws_connect(_url(b) + "/gfs/mailbox/ws")
            hello = await ws.receive_json(timeout=2)
            assert hello["gfs_instance_id"] == "gfs-other"
            await ws.send_json(box.claim(hello["challenge"], gfs_id="gfs-shared"))
            assert (await ws.receive_json(timeout=2))["type"] == "mailbox_claimed"
            await ws.close()
    finally:
        await b.close()
        await a.close()

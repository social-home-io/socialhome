"""GFS mailboxes — review rounds 3/4 (#1326): no pool ever evicts (tail-drop
plus a per-recipient reserve),
running per-pool totals, an atomic daily cap, persisted sibling ids, no
target id in relay-queue logs, the GFS id in the mailbox key derivation."""

from __future__ import annotations

import asyncio
import logging

import pytest

from socialhome.federation.gfs_mailbox import derive_mailbox_keys
from socialhome.global_server.config import EnvelopeMailboxConfig
from socialhome.global_server.envelope_relay import GfsEnvelopeRelay
from socialhome.global_server.mailboxes import (
    DAY_SECONDS,
    GfsMailboxService,
    address_scopes,
)
from socialhome.global_server.repositories import (
    QUEUE_RESERVE_BYTES,
    QUEUE_RESERVE_ROWS,
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsFederationRepo,
    SqliteGfsMailboxRepo,
)
from socialhome.global_server.ws_registry import GfsWebSocketRegistry

TODAY = 100 * DAY_SECONDS
YESTERDAY = TODAY - DAY_SECONDS


async def _put(q, to, n, *, mailbox, total, ceiling=None, now=TODAY + 10, ttl=10**6):
    return await q.enqueue(
        to,
        '{"x":"' + "x" * (n - 8) + '"}',  # n bytes of a JSON object
        created_at=now,
        expires_at=now + ttl,
        max_per_recipient=1000,
        max_bytes_per_recipient=10**8,
        max_total_bytes=total,
        ceiling_bytes=ceiling,
        mailbox_pool=mailbox,
    )


async def _boxes(gfs_db, *ids: str):
    repo = SqliteGfsMailboxRepo(gfs_db)
    for mid in ids:
        await repo.register(mid, b"\x01" * 32, now=TODAY)
    return SqliteGfsEnvelopeQueueRepo(gfs_db)


# ── 1 + 2. Both pools: tail-drop plus a per-recipient reserve ───────────
#
# (Round 3's ranked mailbox eviction was removed — keep it simple: no pool
# ever deletes a live row to make room.)


@pytest.mark.security
@pytest.mark.parametrize("mailbox", [False, True])
async def test_nobody_can_delete_anyones_rows_by_filling_the_pool(gfs_db, mailbox):
    victim = "v" * 32
    flood = [f"{i:032d}" for i in range(30)]
    q = await _boxes(gfs_db, victim, *flood)
    row = 4 * 1024
    for _ in range(25):
        assert await _put(
            q, victim, row, mailbox=mailbox, total=40 * row, ceiling=80 * row
        )
    for _ in range(5):
        for f in flood:
            await _put(q, f, row, mailbox=mailbox, total=40 * row, ceiling=80 * row)
    assert await q.count_for(victim) == 25


@pytest.mark.parametrize("mailbox", [False, True])
async def test_a_full_pool_still_admits_a_recipient_under_its_reserve(gfs_db, mailbox):
    big, late = "b" * 32, "l" * 32
    q = await _boxes(gfs_db, big, late)
    row = 2 * 1024
    total = 30 * row
    for _ in range(30):
        assert await _put(q, big, row, mailbox=mailbox, total=total, ceiling=100 * row)
    # The pool is full; ``big`` is over its reserve → tail-dropped …
    assert not await _put(q, big, row, mailbox=mailbox, total=total, ceiling=100 * row)
    # … while a recipient under its reserve still gets its envelopes in,
    # up to QUEUE_RESERVE_ROWS of them.
    admitted = [
        await _put(q, late, row, mailbox=mailbox, total=total, ceiling=100 * row)
        for _ in range(QUEUE_RESERVE_ROWS + 3)
    ]
    assert admitted == [True] * QUEUE_RESERVE_ROWS + [False] * 3


async def test_a_reserve_counts_bytes_too_and_stops_at_the_ceiling(gfs_db):
    a, b = "a" * 32, "b" * 32
    q = await _boxes(gfs_db, a, b)
    big = QUEUE_RESERVE_BYTES  # one envelope reaches the byte reserve
    assert await _put(q, a, 10, mailbox=True, total=10, ceiling=10**6)
    assert await _put(q, b, big, mailbox=True, total=10, ceiling=10**6)
    assert not await _put(q, b, 100, mailbox=True, total=10, ceiling=10**6)
    # The ceiling bounds the reserves themselves.
    c = "c" * 32
    await _boxes(gfs_db, c)
    assert not await _put(q, c, 100, mailbox=True, total=10, ceiling=big + 50)


async def test_expired_rows_never_hold_room(gfs_db):
    a, b = "a" * 32, "b" * 32
    q = await _boxes(gfs_db, a, b)
    for _ in range(30):
        await _put(q, a, 1000, mailbox=False, total=30_000, ttl=5)
    # Later, a's rows have expired: the pool sweeps them before refusing.
    assert await _put(q, b, 25_000, mailbox=False, total=30_000, now=TODAY + 100)
    assert await q.pool_bytes("envelope", "household") == 25_000


async def test_drift_repair_recomputes_the_totals(gfs_db):
    q = await _boxes(gfs_db, "m" * 32)
    await _put(q, "h" * 32, 100, mailbox=False, total=10**6)
    await _put(q, "m" * 32, 70, mailbox=True, total=10**6)
    await gfs_db.enqueue("UPDATE gfs_queue_totals SET bytes = 999999")
    await q.recompute_pool_totals()
    assert await q.pool_bytes("envelope", "household") == 100
    assert await q.pool_bytes("envelope", "mailbox") == 70
    assert await q.pool_bytes("media", "mailbox") == 0


# ── 4. Running totals, index, atomic daily cap ─────────────────────────


async def test_pool_totals_track_every_insert_and_delete(gfs_db):
    q = await _boxes(gfs_db, "m" * 32)
    await _put(q, "h" * 32, 100, mailbox=False, total=10**6)
    await _put(q, "m" * 32, 70, mailbox=True, total=10**6)
    await q.enqueue_media(
        "m" * 32,
        b"y" * 50,
        created_at=1,
        expires_at=10**10,
        max_bytes_per_recipient=10**6,
        max_total_bytes=10**6,
        mailbox_pool=True,
    )
    assert await q.pool_bytes("envelope", "household") == 100
    assert await q.pool_bytes("envelope", "mailbox") == 70
    assert await q.pool_bytes("media", "mailbox") == 50
    rows = await q.list_for("h" * 32, now=0)
    await q.delete(rows[0].id)
    await SqliteGfsMailboxRepo(gfs_db).prune_idle(older_than=TODAY + 1)
    assert await q.pool_bytes("envelope", "household") == 0
    assert await q.pool_bytes("envelope", "mailbox") == 0
    assert await q.pool_bytes("media", "mailbox") == 0


async def test_count_created_since_has_an_index(gfs_db):
    rows = await gfs_db.fetchall("PRAGMA index_list(gfs_mailboxes)")
    names = {r["name"] for r in rows}
    assert "idx_gfs_mailboxes_created" in names


async def test_the_daily_cap_is_checked_in_the_insert_transaction(gfs_db):
    repo = SqliteGfsMailboxRepo(gfs_db)
    outcomes = await asyncio.gather(
        *(
            repo.register(f"{i:032d}", b"\x01" * 32, now=TODAY, max_new_today=3)
            for i in range(8)
        )
    )
    assert sorted(outcomes).count("created") == 3
    assert outcomes.count("server_cap") == 5


# ── 5. Supernet multipliers ────────────────────────────────────────────


def test_supernet_multipliers_leave_room_for_neighbours():
    assert [m for _k, m in address_scopes("2001:db8:1:2::/64")] == [1, 16, 256]


# ── 6. Sibling ids are remembered in the shared DB ─────────────────────


async def test_a_dead_siblings_id_is_still_accepted(gfs_db):
    seen = {"gfs-dead"}
    svc = GfsMailboxService(
        repo=SqliteGfsMailboxRepo(gfs_db),
        gfs_instance_id="gfs-1",
        config=EnvelopeMailboxConfig(),
        sibling_ids=lambda: set(seen),
        clock=lambda: float(TODAY),
    )
    assert "gfs-dead" in await svc.accepted_gfs_ids()
    seen.clear()
    # Another node (another service on the same DB) learns it from the table.
    other = GfsMailboxService(
        repo=SqliteGfsMailboxRepo(gfs_db),
        gfs_instance_id="gfs-2",
        config=EnvelopeMailboxConfig(),
        sibling_ids=lambda: set(),
        clock=lambda: float(TODAY + 30 * DAY_SECONDS),
    )
    assert {"gfs-1", "gfs-dead"} <= await other.accepted_gfs_ids()


# ── 7. Relay-queue logs name no target ─────────────────────────────────


@pytest.mark.security
async def test_relay_queue_drop_logs_name_no_target(gfs_db, caplog):
    caplog.set_level(logging.DEBUG)
    relay = GfsEnvelopeRelay(
        fed_repo=SqliteGfsFederationRepo(gfs_db),
        queue_repo=SqliteGfsEnvelopeQueueRepo(gfs_db),
        ws_registry=GfsWebSocketRegistry(),
    )
    target = "t" * 32
    huge = {"space_id": "s", "event_type": "e", "payload": "x" * (5 * 1024 * 1024)}
    assert await relay.queue_relay(target, huge) is False
    assert any("exceeds the relay queue" in r.getMessage() for r in caplog.records)
    assert all(target not in r.getMessage() for r in caplog.records)


# ── 8. The GFS id is part of the mailbox key derivation ────────────────


def test_two_gfss_give_a_household_different_mailboxes():
    root = bytes(range(32))
    a = derive_mailbox_keys(root, "gfs-a", "space-1", 0)
    b = derive_mailbox_keys(root, "gfs-b", "space-1", 0)
    assert a.mailbox_id != b.mailbox_id
    assert a.kem_public != b.kem_public
    with pytest.raises(ValueError):
        derive_mailbox_keys(root, "gfs|x", "space-1", 0)

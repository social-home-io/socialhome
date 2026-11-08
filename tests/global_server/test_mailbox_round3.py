"""GFS mailboxes — review round 3 (#1326): no anonymous delete primitive
in the household pool, sybil-resistant mailbox eviction with a floor,
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
    MAILBOX_QUEUE_FLOOR_BYTES,
    MAILBOX_QUEUE_FLOOR_ROWS,
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsFederationRepo,
    SqliteGfsMailboxRepo,
)
from socialhome.global_server.ws_registry import GfsWebSocketRegistry

TODAY = 100 * DAY_SECONDS
YESTERDAY = TODAY - DAY_SECONDS


async def _put(q, to, n, *, mailbox, total, now=TODAY + 10):
    return await q.enqueue(
        to,
        '{"x":"' + "x" * (n - 8) + '"}',  # n bytes of a JSON object
        created_at=now,
        expires_at=now + 10**6,
        max_per_recipient=1000,
        max_bytes_per_recipient=10**8,
        max_total_bytes=total,
        mailbox_pool=mailbox,
    )


# ── 1. Household pool: tail-drop, never a delete primitive ──────────────


@pytest.mark.security
async def test_posting_to_your_own_id_never_removes_a_households_envelopes(gfs_db):
    q = SqliteGfsEnvelopeQueueRepo(gfs_db)
    victim, attacker = "v" * 32, "a" * 32
    for _ in range(3):
        assert await _put(q, victim, 300, mailbox=False, total=1000)
    # The pool is (nearly) full: the attacker's envelope is the one dropped.
    assert not await _put(q, attacker, 200, mailbox=False, total=1000)
    assert await q.count_for(victim) == 3


# ── 2. Mailbox pool: sybils can't starve an established mailbox ─────────


async def _boxes(gfs_db, **created):
    repo = SqliteGfsMailboxRepo(gfs_db)
    for mid, (day, claimed) in created.items():
        await repo.register(mid * 32 if len(mid) == 1 else mid, b"\x01" * 32, now=day)
        if claimed:
            await repo.touch_claim(mid * 32 if len(mid) == 1 else mid, now=claimed)
    return SqliteGfsEnvelopeQueueRepo(gfs_db)


@pytest.mark.security
async def test_sybils_created_today_cannot_starve_an_established_mailbox(gfs_db):
    sybils = {chr(ord("a") + i): (TODAY, TODAY) for i in range(9)}
    q = await _boxes(gfs_db, z=(YESTERDAY, TODAY), **sybils)
    legit = "z" * 32
    row = 4 * 1024
    total = 60 * row
    for _ in range(9):
        assert await _put(q, legit, row, mailbox=True, total=total)
    for _ in range(20):
        for s in sybils:
            await _put(q, s * 32, row, mailbox=True, total=total)
    assert await q.count_for(legit) == 9


async def test_never_claimed_mailboxes_are_evicted_first(gfs_db):
    q = await _boxes(
        gfs_db,
        w=(YESTERDAY, TODAY),  # writer
        c=(YESTERDAY, TODAY),  # claimed, big
        n=(YESTERDAY, None),  # never claimed, smaller
    )
    row = 8 * 1024
    floor = MAILBOX_QUEUE_FLOOR_ROWS + 5
    total = (2 * floor + 10) * row
    for _ in range(floor + 10):
        assert await _put(q, "c" * 32, row, mailbox=True, total=total)
    for _ in range(floor):
        assert await _put(q, "n" * 32, row, mailbox=True, total=total)
    before_c = await q.count_for("c" * 32)
    assert await _put(q, "w" * 32, row, mailbox=True, total=total)
    assert await q.count_for("c" * 32) == before_c
    assert await q.count_for("n" * 32) == floor - 1


async def test_every_mailbox_keeps_a_floor(gfs_db):
    q = await _boxes(gfs_db, w=(YESTERDAY, TODAY), v=(YESTERDAY, TODAY))
    small = 1024
    total = 25 * small
    for _ in range(25):
        assert await _put(q, "v" * 32, small, mailbox=True, total=total)
    for _ in range(30):
        await _put(q, "w" * 32, small, mailbox=True, total=total)
    assert await q.count_for("v" * 32) >= MAILBOX_QUEUE_FLOOR_ROWS
    assert MAILBOX_QUEUE_FLOOR_BYTES == 64 * 1024


# ── 4. Running totals, index, atomic daily cap ─────────────────────────


async def test_pool_totals_track_every_insert_and_delete(gfs_db):
    q = await _boxes(gfs_db, m=(TODAY, None))
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

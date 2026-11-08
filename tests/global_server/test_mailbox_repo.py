"""``SqliteGfsMailboxRepo`` against a real migrated GFS database."""

from __future__ import annotations

import pytest

from socialhome.global_server.repositories import (
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsMailboxRepo,
)

MID = "a" * 32
PK = b"\x01" * 32
_seq = iter(range(1, 10**6))


def _t() -> dict:
    """A fresh ticket nonce for one register."""
    return {"ticket_nonce": next(_seq).to_bytes(32, "big"), "ticket_expires_at": 10**10}


@pytest.fixture
async def repo(gfs_db):
    return SqliteGfsMailboxRepo(gfs_db)


async def test_register_pins_the_key_on_first_use(repo):
    assert await repo.register(MID, PK, now=100, **_t()) == "created"
    assert await repo.register(MID, PK, now=200, **_t()) == "exists"
    box = await repo.get(MID)
    assert box is not None
    assert box.sign_pk == PK
    assert box.created_at == 100  # the first registration is kept
    assert box.last_claim_at is None
    assert await repo.get("b" * 32) is None


async def test_touch_claim_records_the_latest_claim(repo):
    await repo.register(MID, PK, now=100, **_t())
    await repo.touch_claim(MID, now=150)
    box = await repo.get(MID)
    assert box is not None and box.last_claim_at == 150


async def test_prune_idle_drops_the_mailbox_and_its_queued_frames(repo, gfs_db):
    queue = SqliteGfsEnvelopeQueueRepo(gfs_db)
    fresh = "c" * 32
    await repo.register(MID, PK, now=100, **_t())  # never claimed, idle since 100
    await repo.register(fresh, b"\x02" * 32, now=100, **_t())
    await repo.touch_claim(fresh, now=1000)
    await queue.enqueue(
        MID,
        "{}",
        created_at=100,
        expires_at=10**10,
        max_per_recipient=10,
        max_bytes_per_recipient=10**6,
    )
    await queue.enqueue_media(
        MID,
        b"\x00SHR",
        created_at=100,
        expires_at=10**10,
        max_bytes_per_recipient=10**6,
        max_total_bytes=10**7,
    )
    assert await repo.prune_idle(older_than=500) == 1
    assert await repo.get(MID) is None
    assert await repo.get(fresh) is not None
    assert await queue.count_for(MID) == 0
    assert await queue.media_bytes_for(MID, now=0) == 0


async def test_is_mailbox(repo):
    await repo.register(MID, PK, now=1, **_t())
    assert await repo.is_mailbox(MID) is True
    assert await repo.is_mailbox("d" * 32) is False


async def _spent(gfs_db) -> int:
    return len(await gfs_db.fetchall("SELECT nonce FROM gfs_spent_tickets"))


async def test_the_ticket_is_spent_in_the_insert_transaction(repo, gfs_db):
    """A ticket nonce is recorded only with a NEW mailbox, in one
    transaction: an existing mailbox or an id an instance holds spends
    nothing, and a nonce spent before creates nothing."""
    ticket = _t()
    assert await repo.register(MID, PK, now=100, **ticket) == "created"
    assert await _spent(gfs_db) == 1
    assert await repo.register(MID, PK, now=100, **_t()) == "exists"
    assert await _spent(gfs_db) == 1
    other = "d" * 32
    assert await repo.register(other, PK, now=100, **ticket) == "ticket_spent"
    assert await repo.get(other) is None
    assert await _spent(gfs_db) == 1


async def test_a_collision_spends_no_ticket(repo, gfs_db):
    await gfs_db.enqueue(
        "INSERT INTO client_instances(instance_id, display_name, public_key, status)"
        " VALUES(?,?,?,?)",
        (MID, "x", "00", "active"),
    )
    assert await repo.register(MID, PK, now=100, **_t()) == "collision"
    assert await _spent(gfs_db) == 0

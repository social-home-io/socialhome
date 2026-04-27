"""Tests for socialhome.repositories.pairing_relay_repo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.pairing_relay_repo import SqlitePairingRelayRepo


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.repo = SqlitePairingRelayRepo(db)
    yield e
    await db.shutdown()


async def test_save_get_roundtrip(env):
    now = datetime.now(timezone.utc)
    await env.repo.save(
        request_id="r-1",
        from_instance="peer-a",
        target_instance_id="peer-b",
        message="hi",
        received_at=now,
    )
    row = await env.repo.get("r-1")
    assert row is not None
    assert row["from_instance"] == "peer-a"
    assert row["target_instance_id"] == "peer-b"
    assert row["message"] == "hi"
    assert row["status"] == "pending"


async def test_list_pending_excludes_non_pending(env):
    now = datetime.now(timezone.utc)
    await env.repo.save(
        request_id="r-1",
        from_instance="a",
        target_instance_id="b",
        message="m1",
        received_at=now,
    )
    await env.repo.save(
        request_id="r-2",
        from_instance="a",
        target_instance_id="b",
        message="m2",
        received_at=now + timedelta(seconds=1),
    )
    await env.repo.set_status("r-1", "approved")
    pending = await env.repo.list_pending()
    assert {p["id"] for p in pending} == {"r-2"}


async def test_set_status_transitions(env):
    now = datetime.now(timezone.utc)
    await env.repo.save(
        request_id="r-1",
        from_instance="a",
        target_instance_id="b",
        message="m",
        received_at=now,
    )
    await env.repo.set_status("r-1", "declined")
    assert await env.repo.get("r-1") is None  # no longer pending


async def test_count_pending(env):
    now = datetime.now(timezone.utc)
    for i in range(3):
        await env.repo.save(
            request_id=f"r-{i}",
            from_instance="a",
            target_instance_id="b",
            message="m",
            received_at=now + timedelta(seconds=i),
        )
    assert await env.repo.count_pending() == 3
    await env.repo.set_status("r-0", "approved")
    assert await env.repo.count_pending() == 2


async def test_delete_oldest_pending_keeps_most_recent(env):
    now = datetime.now(timezone.utc)
    for i in range(5):
        await env.repo.save(
            request_id=f"r-{i}",
            from_instance="a",
            target_instance_id="b",
            message="m",
            received_at=now + timedelta(seconds=i),
        )
    deleted = await env.repo.delete_oldest_pending(keep=2)
    assert deleted == 3
    pending = await env.repo.list_pending()
    assert {p["id"] for p in pending} == {"r-3", "r-4"}


async def test_delete_older_than_filters_by_status_and_age(env):
    now = datetime.now(timezone.utc)

    async def _seed(rid, status, dt):
        await env.db.enqueue(
            """
            INSERT INTO pairing_relay(
                id, from_instance, target_instance_id, message,
                received_at, status
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (rid, "a", "b", "m", dt.isoformat(), status),
        )

    await _seed("old-approved", "approved", now - timedelta(days=10))
    await _seed("new-approved", "approved", now - timedelta(days=2))
    await _seed("old-declined", "declined", now - timedelta(days=10))

    cutoff = (now - timedelta(days=7)).isoformat()
    purged_a = await env.repo.delete_older_than(status="approved", cutoff_iso=cutoff)
    assert purged_a == 1

    rows = await env.db.fetchall("SELECT id FROM pairing_relay ORDER BY id")
    assert {r["id"] for r in rows} == {"new-approved", "old-declined"}

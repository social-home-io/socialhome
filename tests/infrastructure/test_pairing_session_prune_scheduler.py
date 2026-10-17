"""Tests for :class:`PairingSessionPruneScheduler` (§11 TTL sweep)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from socialhome.infrastructure.pairing_session_prune_scheduler import (
    PairingSessionPruneScheduler,
)
from socialhome.repositories.federation_repo import SqliteFederationRepo


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
    e.repo = SqliteFederationRepo(db)
    yield e
    await db.shutdown()


async def _seed_expired_pair(env, *, token: str, local_inbox_id: str) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await env.repo.create_pairing(
        PairingSession(
            token=token,
            own_identity_pk="aa" * 32,
            own_dh_pk="bb" * 32,
            own_dh_sk="cc" * 32,
            inbox_url=f"https://local/inbox/{local_inbox_id}",
            own_local_inbox_id=local_inbox_id,
            issued_at=past,
            expires_at=past,
            status=PairingStatus.PENDING_RECEIVED,
        ),
    )
    await env.repo.save_instance(
        RemoteInstance(
            id=f"peer-{token}",
            display_name=f"Stale-{token}",
            remote_identity_pk="11" * 32,
            key_self_to_remote="k1",
            key_remote_to_self="k2",
            remote_inbox_url=f"https://peer/{token}",
            local_inbox_id=local_inbox_id,
            status=PairingStatus.PENDING_RECEIVED,
        ),
    )


async def test_prune_once_drops_expired_sessions_and_their_orphan_instances(env):
    await _seed_expired_pair(env, token="t1", local_inbox_id="inbox-1")
    await _seed_expired_pair(env, token="t2", local_inbox_id="inbox-2")

    sched = PairingSessionPruneScheduler(env.repo, interval_seconds=60.0)
    pruned = await sched._prune_once()

    assert pruned == 2
    assert await env.repo.get_pairing("t1") is None
    assert await env.repo.get_pairing("t2") is None
    assert await env.repo.get_instance("peer-t1") is None
    assert await env.repo.get_instance("peer-t2") is None


async def test_prune_once_keeps_fresh_sessions(env):
    future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    await env.repo.create_pairing(
        PairingSession(
            token="fresh",
            own_identity_pk="aa" * 32,
            own_dh_pk="bb" * 32,
            own_dh_sk="cc" * 32,
            inbox_url="https://local/inbox/fresh",
            own_local_inbox_id="fresh",
            issued_at=datetime.now(UTC).isoformat(),
            expires_at=future,
            status=PairingStatus.PENDING_SENT,
        ),
    )

    sched = PairingSessionPruneScheduler(env.repo, interval_seconds=60.0)
    assert await sched._prune_once() == 0
    assert await env.repo.get_pairing("fresh") is not None


async def test_prune_once_empty_table_is_zero(env):
    sched = PairingSessionPruneScheduler(env.repo)
    assert await sched._prune_once() == 0


async def test_double_start_is_idempotent(env):
    sched = PairingSessionPruneScheduler(env.repo, interval_seconds=10.0)
    await sched.start()
    await sched.start()
    await sched.stop()


async def test_stop_without_start_is_safe(env):
    sched = PairingSessionPruneScheduler(env.repo)
    await sched.stop()


async def test_loop_ticks_periodically_and_prunes(env):
    """A short interval lets the loop tick at least once and remove
    the expired row without the test having to drive the prune call."""
    await _seed_expired_pair(env, token="loop-1", local_inbox_id="loop-1")
    sched = PairingSessionPruneScheduler(env.repo, interval_seconds=0.05)
    await sched.start()
    await asyncio.sleep(0.15)
    await sched.stop()
    assert await env.repo.get_pairing("loop-1") is None

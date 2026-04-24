"""Tests for socialhome.repositories.federation_repo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.federation import (
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from socialhome.repositories.federation_repo import SqliteFederationRepo


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a federation repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

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
    e.iid = iid
    e.fed_repo = SqliteFederationRepo(db)
    yield e
    await db.shutdown()


async def test_federation_pairing_lifecycle(env):
    """Create, read, update, then delete a pairing session."""
    now = datetime.now(timezone.utc).isoformat()
    session = PairingSession(
        token="tok-abc",
        own_identity_pk="aa" * 32,
        own_dh_pk="bb" * 32,
        own_dh_sk="cc" * 32,
        inbox_url="https://local/inbox",
        issued_at=now,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        status=PairingStatus.PENDING_SENT,
    )
    await env.fed_repo.create_pairing(session)

    got = await env.fed_repo.get_pairing("tok-abc")
    assert got is not None
    assert got.token == "tok-abc"
    assert got.status == PairingStatus.PENDING_SENT

    updated_session = PairingSession(
        token="tok-abc",
        own_identity_pk=session.own_identity_pk,
        own_dh_pk=session.own_dh_pk,
        own_dh_sk=session.own_dh_sk,
        inbox_url=session.inbox_url,
        peer_identity_pk="dd" * 32,
        peer_dh_pk="ee" * 32,
        peer_inbox_url="https://peer/inbox",
        issued_at=now,
        expires_at=session.expires_at,
        status=PairingStatus.PENDING_RECEIVED,
    )
    await env.fed_repo.update_pairing(updated_session)
    refreshed = await env.fed_repo.get_pairing("tok-abc")
    assert refreshed.status == PairingStatus.PENDING_RECEIVED
    assert refreshed.peer_inbox_url == "https://peer/inbox"

    await env.fed_repo.delete_pairing("tok-abc")
    assert await env.fed_repo.get_pairing("tok-abc") is None


async def test_federation_replay_cache(env):
    """Insert replay IDs and confirm they appear in load_replay_cache; prune works."""
    await env.fed_repo.insert_replay_id("msg-001")
    await env.fed_repo.insert_replay_id("msg-002")

    entries = await env.fed_repo.load_replay_cache(within_hours=1)
    msg_ids = {e[0] for e in entries}
    assert "msg-001" in msg_ids
    assert "msg-002" in msg_ids

    await env.fed_repo.insert_replay_id("msg-001")

    yesterday = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    removed = await env.fed_repo.prune_replay_cache(yesterday)
    assert removed == 0

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    removed_all = await env.fed_repo.prune_replay_cache(future)
    assert removed_all >= 2


async def test_federation_instance_filtering(env):
    """Save two instances, filter by status, mark unreachable/reachable, then delete."""
    inst1 = RemoteInstance(
        id="peer-001",
        display_name="Alpha",
        remote_identity_pk="11" * 32,
        key_self_to_remote="k1",
        key_remote_to_self="k2",
        remote_inbox_url="https://alpha/wh",
        local_inbox_id="wh-1",
        status=PairingStatus.CONFIRMED,
    )
    inst2 = RemoteInstance(
        id="peer-002",
        display_name="Beta",
        remote_identity_pk="22" * 32,
        key_self_to_remote="k3",
        key_remote_to_self="k4",
        remote_inbox_url="https://beta/wh",
        local_inbox_id="wh-2",
        status=PairingStatus.UNPAIRING,
    )
    await env.fed_repo.save_instance(inst1)
    await env.fed_repo.save_instance(inst2)

    confirmed = await env.fed_repo.list_instances(status="confirmed")
    confirmed_ids = {i.id for i in confirmed}
    assert "peer-001" in confirmed_ids
    assert "peer-002" not in confirmed_ids

    await env.fed_repo.mark_unreachable("peer-001")
    got = await env.fed_repo.get_instance("peer-001")
    assert not got.is_reachable()

    await env.fed_repo.mark_reachable("peer-001")
    assert (await env.fed_repo.get_instance("peer-001")).is_reachable()

    await env.fed_repo.delete_instance("peer-002")
    assert await env.fed_repo.get_instance("peer-002") is None

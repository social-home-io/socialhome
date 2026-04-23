"""Tests for SqliteDmRoutingRepo — §12.5 DM reliability bits.

Covers the newly-added gap persistence + relay-path listing surface.
Existing network_discovery / conversation_sender_sequences / dedup
coverage lives in test_dm_routing_service.py (service-level).
"""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo


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
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES('c1', 'dm', datetime('now'))",
    )

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteDmRoutingRepo(db)
    yield e
    await db.shutdown()


# ── peek_sender_seq ────────────────────────────────────────────────────────


async def test_peek_sender_seq_default_zero(env):
    seq = await env.repo.peek_sender_seq(conversation_id="c1", sender_user_id="uid-a")
    assert seq == 0


async def test_peek_sender_seq_reflects_next_seq(env):
    n1 = await env.repo.next_sender_seq(conversation_id="c1", sender_user_id="uid-a")
    n2 = await env.repo.next_sender_seq(conversation_id="c1", sender_user_id="uid-a")
    assert (n1, n2) == (1, 2)
    peek = await env.repo.peek_sender_seq(conversation_id="c1", sender_user_id="uid-a")
    assert peek == 2


# ── Gap persistence ────────────────────────────────────────────────────────


async def test_insert_and_list_gaps(env):
    await env.repo.insert_gaps(
        conversation_id="c1",
        sender_user_id="uid-a",
        expected_seqs=[2, 3, 5],
    )
    gaps = await env.repo.list_open_gaps("c1")
    assert [g["expected_seq"] for g in gaps] == [2, 3, 5]
    assert all(g["sender_user_id"] == "uid-a" for g in gaps)


async def test_insert_gaps_is_idempotent(env):
    await env.repo.insert_gaps(
        conversation_id="c1", sender_user_id="uid-a", expected_seqs=[2, 3]
    )
    await env.repo.insert_gaps(
        conversation_id="c1", sender_user_id="uid-a", expected_seqs=[2, 3]
    )
    gaps = await env.repo.list_open_gaps("c1")
    assert len(gaps) == 2


async def test_resolve_gap_removes_one(env):
    await env.repo.insert_gaps(
        conversation_id="c1", sender_user_id="uid-a", expected_seqs=[2, 3, 4]
    )
    await env.repo.resolve_gap(
        conversation_id="c1", sender_user_id="uid-a", expected_seq=3
    )
    gaps = await env.repo.list_open_gaps("c1")
    assert [g["expected_seq"] for g in gaps] == [2, 4]


# ── Relay-path listing ─────────────────────────────────────────────────────


async def test_list_relay_paths(env):
    await env.repo.upsert_conversation_path(
        conversation_id="c1",
        target_instance="inst-b",
        relay_via="inst-hub",
        hop_count=2,
        last_used_at="2026-01-01T00:00:00Z",
    )
    await env.repo.upsert_conversation_path(
        conversation_id="c1",
        target_instance="inst-c",
        relay_via="inst-b",
        hop_count=3,
        last_used_at="2026-01-02T00:00:00Z",
    )
    paths = await env.repo.list_relay_paths("c1")
    assert [p["target_instance"] for p in paths] == ["inst-b", "inst-c"]
    assert paths[0]["hop_count"] == 2


async def test_list_relay_paths_empty(env):
    assert await env.repo.list_relay_paths("c1") == []

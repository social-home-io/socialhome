"""Tests for SqliteAliasRepo (§4.1.6)."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.alias_repo import (
    MAX_ALIAS_LENGTH,
    SqliteAliasRepo,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    # Two local users to play viewer + target.
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name)"
        " VALUES('alice', 'uid-alice', 'Alice')",
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name)"
        " VALUES('bob', 'uid-bob', 'Bob')",
    )
    yield db, SqliteAliasRepo(db)
    await db.shutdown()


async def test_set_and_get_user_alias(env):
    _, repo = env
    await repo.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="Mr B",
    )
    got = await repo.get_user_aliases("uid-alice", ["uid-bob"])
    assert got == {"uid-bob": "Mr B"}


async def test_set_user_alias_replaces_existing(env):
    _, repo = env
    await repo.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="B1",
    )
    await repo.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="B2",
    )
    got = await repo.get_user_aliases("uid-alice", ["uid-bob"])
    assert got == {"uid-bob": "B2"}


async def test_clear_user_alias(env):
    _, repo = env
    await repo.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="B",
    )
    await repo.clear_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
    )
    assert await repo.get_user_aliases("uid-alice", ["uid-bob"]) == {}


async def test_clear_unknown_alias_is_noop(env):
    _, repo = env
    # No row exists — should not raise.
    await repo.clear_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
    )


async def test_get_user_aliases_bulk_lookup(env):
    """Bulk lookup short-circuits empty inputs and filters by viewer."""
    _, repo = env
    # alice's aliases — only one of two requested IDs has one set.
    await repo.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="B-from-alice",
    )
    # bob's view of alice — must NOT leak into alice's lookup.
    await repo.set_user_alias(
        viewer_user_id="uid-bob",
        target_user_id="uid-alice",
        alias="A-from-bob",
    )
    got = await repo.get_user_aliases("uid-alice", ["uid-bob", "uid-ghost"])
    assert got == {"uid-bob": "B-from-alice"}
    # Empty input → empty dict, no SQL.
    assert await repo.get_user_aliases("uid-alice", []) == {}


async def test_aliases_are_per_viewer(env):
    """alice and bob each see their own aliases for the same target."""
    _, repo = env
    await repo.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="alice-says-B",
    )
    await repo.set_user_alias(
        viewer_user_id="uid-bob",
        target_user_id="uid-bob",
        alias="bob-says-B",
    )
    assert await repo.get_user_aliases("uid-alice", ["uid-bob"]) == {
        "uid-bob": "alice-says-B",
    }
    assert await repo.get_user_aliases("uid-bob", ["uid-bob"]) == {
        "uid-bob": "bob-says-B",
    }


async def test_list_user_aliases_returns_all_for_viewer(env):
    _, repo = env
    await repo.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-bob",
        alias="B",
    )
    await repo.set_user_alias(
        viewer_user_id="uid-alice",
        target_user_id="uid-c",
        alias="C",
    )
    rows = await repo.list_user_aliases("uid-alice")
    assert rows == {"uid-bob": "B", "uid-c": "C"}


async def test_alias_length_constraint_enforced_by_db(env):
    """Repo trusts service-layer validation; DB constraint is the safety net."""
    db, repo = env
    huge = "x" * (MAX_ALIAS_LENGTH + 1)
    with pytest.raises(Exception):  # IntegrityError from CHECK constraint
        await db.enqueue(
            "INSERT INTO user_aliases(viewer_user_id, target_user_id, alias)"
            " VALUES(?, ?, ?)",
            ("uid-alice", "uid-bob", huge),
        )
        # Force-flush.
        await db.fetchall("SELECT 1")

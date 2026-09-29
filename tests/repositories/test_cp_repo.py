"""Tests for :class:`SqliteCpRepo` — the guardian-block reads (§CP.F2)."""

from __future__ import annotations

import pytest

from socialhome.repositories.cp_repo import (
    SqliteCpRepo,
    guardian_block_counterparts_sql,
)


@pytest.fixture
async def repo(db):
    for name, protected in (("kid", 1), ("bob", 0), ("sis", 1), ("amy", 0)):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name,"
            " child_protection_enabled) VALUES(?,?,?,?)",
            (name, f"u-{name}", name.title(), protected),
        )
    r = SqliteCpRepo(db)
    await r.block_user(
        minor_user_id="u-kid", blocked_user_id="u-bob", blocked_by="u-amy"
    )
    await r.block_user(
        minor_user_id="u-kid", blocked_user_id="remote-rex", blocked_by="u-amy"
    )
    return r


async def test_blocked_pair_holds_in_both_directions(repo):
    assert await repo.is_blocked_pair("u-kid", "u-bob")
    assert await repo.is_blocked_pair("u-bob", "u-kid")
    assert await repo.is_blocked_pair("remote-rex", "u-kid")


async def test_unrelated_pairs_are_not_blocked(repo):
    assert not await repo.is_blocked_pair("u-kid", "u-amy")
    assert not await repo.is_blocked_pair("u-bob", "u-amy")
    assert not await repo.is_blocked_pair("u-sis", "u-bob")


async def test_counterparts_cover_both_sides(repo):
    assert await repo.list_block_counterparts("u-kid") == frozenset(
        {"u-bob", "remote-rex"}
    )
    assert await repo.list_block_counterparts("u-bob") == frozenset({"u-kid"})
    assert await repo.list_block_counterparts("u-amy") == frozenset()


async def test_a_block_counts_only_while_the_account_is_protected(repo, db):
    await repo.disable_protection("kid")
    assert not await repo.is_blocked_pair("u-kid", "u-bob")
    assert await repo.list_block_counterparts("u-bob") == frozenset()
    assert await repo.list_block_counterparts("u-kid") == frozenset()
    # The block itself is kept for when protection comes back.
    assert await repo.is_blocked_for_minor("u-kid", "u-bob")


async def test_unblock_clears_the_pair(repo):
    await repo.unblock_user(minor_user_id="u-kid", blocked_user_id="u-bob")
    assert not await repo.is_blocked_pair("u-bob", "u-kid")
    assert await repo.list_block_counterparts("u-kid") == frozenset({"remote-rex"})


async def test_blocks_someone_homed_on_a_household(repo, db):
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES('peer-x', 'X', ?, 'k1', 'k2',"
        " 'https://x/wh', 'wh-x', 'confirmed', 'manual')",
        ("00" * 32,),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('remote-rex', 'peer-x', 'rex', 'Rex')"
    )
    assert await repo.blocks_someone_homed_on("peer-x")
    assert await repo.blocks_someone_homed_on("peer-x", minor_user_id="u-kid")
    assert not await repo.blocks_someone_homed_on("peer-x", minor_user_id="u-sis")
    assert not await repo.blocks_someone_homed_on("peer-other")
    await repo.disable_protection("kid")
    assert not await repo.blocks_someone_homed_on("peer-x")


async def test_counterparts_sql_takes_an_outer_column(repo, db):
    rows = await db.fetchall(
        "SELECT u.username FROM users u WHERE 'u-bob' IN ("
        + guardian_block_counterparts_sql("u.user_id")
        + ")"
    )
    assert [r["username"] for r in rows] == ["kid"]

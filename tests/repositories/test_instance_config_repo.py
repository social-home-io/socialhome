"""Tests for SqliteInstanceConfigRepo (the ``instance_config`` kv table)."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.repositories.instance_config_repo import (
    AbstractInstanceConfigRepo,
    SqliteInstanceConfigRepo,
)


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    yield SqliteInstanceConfigRepo(db)
    await db.shutdown()


async def test_satisfies_protocol(repo):
    assert isinstance(repo, AbstractInstanceConfigRepo)


async def test_get_missing_returns_none(repo):
    assert await repo.get("nope") is None


async def test_set_then_get(repo):
    await repo.set("dashboard.epoch", "1")
    assert await repo.get("dashboard.epoch") == "1"


async def test_set_upserts(repo):
    await repo.set("k", "a")
    await repo.set("k", "b")
    assert await repo.get("k") == "b"


async def test_delete(repo):
    await repo.set("k", "a")
    await repo.delete("k")
    assert await repo.get("k") is None


async def test_delete_missing_is_noop(repo):
    await repo.delete("never-set")
    assert await repo.get("never-set") is None


async def test_get_many_by_prefix(repo):
    await repo.set("dashboard.a", "1")
    await repo.set("dashboard.b", "2")
    await repo.set("setup_complete", "1")
    assert await repo.get_many("dashboard.") == {
        "dashboard.a": "1",
        "dashboard.b": "2",
    }


async def test_get_many_treats_like_wildcards_literally(repo):
    """``%`` and ``_`` in the prefix match only themselves."""
    await repo.set("a_b.x", "1")
    await repo.set("aXb.y", "2")
    await repo.set("100%.z", "3")
    await repo.set("1000.w", "4")
    assert await repo.get_many("a_b.") == {"a_b.x": "1"}
    assert await repo.get_many("100%") == {"100%.z": "3"}


async def test_get_many_empty_prefix_returns_all(repo):
    await repo.set("x", "1")
    result = await repo.get_many("")
    assert result["x"] == "1"


async def test_set_many_upserts_all(repo):
    await repo.set("a", "old")
    await repo.set_many({"a": "1", "b": "2"})
    assert await repo.get("a") == "1"
    assert await repo.get("b") == "2"


async def test_set_many_empty_is_noop(repo):
    await repo.set_many({})
    assert await repo.get_many("") == {}


async def test_set_many_is_atomic(repo):
    """A failing row rolls back every row of the batch."""
    await repo.set("a", "old")
    with pytest.raises(Exception):
        await repo.set_many({"a": "new", "b": object()})  # type: ignore[dict-item]
    assert await repo.get("a") == "old"
    assert await repo.get("b") is None

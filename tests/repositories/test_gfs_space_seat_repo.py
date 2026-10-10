"""Direct SQLite tests for ``SqliteGfsSpaceSeatRepo`` (migration 0090)."""

from __future__ import annotations

import pytest

from socialhome.repositories.gfs_space_seat_repo import (
    AbstractGfsSpaceSeatRepo,
    SqliteGfsSpaceSeatRepo,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def repo(db):
    return SqliteGfsSpaceSeatRepo(db)


async def test_satisfies_the_protocol(repo):
    assert isinstance(repo, AbstractGfsSpaceSeatRepo)


async def test_record_is_idempotent_and_listed_both_ways(repo):
    await repo.record("sp-1", "gfs-a")
    await repo.record("sp-1", "gfs-a")
    await repo.record("sp-1", "gfs-b")
    await repo.record("sp-2", "gfs-a")
    assert await repo.list_for_space("sp-1") == ["gfs-a", "gfs-b"]
    assert await repo.list_for_gfs("gfs-a") == ["sp-1", "sp-2"]
    assert await repo.list_for_space("sp-none") == []


async def test_forget_drops_only_that_seat(repo):
    await repo.record("sp-1", "gfs-a")
    await repo.record("sp-1", "gfs-b")
    await repo.forget("sp-1", "gfs-a")
    await repo.forget("sp-1", "gfs-zzz")  # absent → no-op
    assert await repo.list_for_space("sp-1") == ["gfs-b"]

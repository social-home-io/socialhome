"""Direct SQLite tests for ``SqliteGfsSpaceSeatRepo`` (migration 0092)."""

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


async def _rec(repo, space_id, gfs, conn="c-1", key="pk-1"):
    await repo.record(space_id, gfs, gfs_connection_id=conn, gfs_public_key=key)


async def test_satisfies_the_protocol(repo):
    assert isinstance(repo, AbstractGfsSpaceSeatRepo)


async def test_record_is_idempotent_and_listed_both_ways(repo):
    await _rec(repo, "sp-1", "gfs-a")
    await _rec(repo, "sp-1", "gfs-a")
    await _rec(repo, "sp-1", "gfs-b")
    await _rec(repo, "sp-2", "gfs-a")
    assert await repo.list_for_space("sp-1") == ["gfs-a", "gfs-b"]
    assert await repo.list_for_gfs("gfs-a") == ["sp-1", "sp-2"]
    assert await repo.list_for_space("sp-none") == []


async def test_re_recording_rebinds_the_connection_and_key(repo):
    await _rec(repo, "sp-1", "gfs-a", conn="c-1", key="pk-1")
    assert await repo.get_binding("sp-1", "gfs-a") == ("c-1", "pk-1")
    await _rec(repo, "sp-1", "gfs-a", conn="c-2", key="pk-2")
    assert await repo.get_binding("sp-1", "gfs-a") == ("c-2", "pk-2")
    assert await repo.get_binding("sp-1", "gfs-zzz") is None


async def test_forget_drops_only_that_seat(repo):
    await _rec(repo, "sp-1", "gfs-a")
    await _rec(repo, "sp-1", "gfs-b")
    await repo.forget("sp-1", "gfs-a")
    await repo.forget("sp-1", "gfs-zzz")  # absent → no-op
    assert await repo.list_for_space("sp-1") == ["gfs-b"]

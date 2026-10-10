"""Direct SQLite tests for ``SqliteGfsSpaceSeatRepo`` (migration 0093)."""

from __future__ import annotations

import pytest

from socialhome.domain.gfs_space_seat import GfsSpaceSeat
from socialhome.repositories.gfs_space_seat_repo import (
    AbstractGfsSpaceSeatRepo,
    SqliteGfsSpaceSeatRepo,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def repo(db):
    return SqliteGfsSpaceSeatRepo(db)


def _s(space_id, gfs, conn="c-1", key="pk-1", url="https://g.test") -> GfsSpaceSeat:
    return GfsSpaceSeat(
        space_id=space_id,
        gfs_instance_id=gfs,
        gfs_connection_id=conn,
        gfs_public_key=key,
        gfs_inbox_url=url,
    )


async def test_satisfies_the_protocol(repo):
    assert isinstance(repo, AbstractGfsSpaceSeatRepo)


async def test_record_is_idempotent_and_listed_both_ways(repo):
    await repo.record(_s("sp-1", "gfs-a"))
    await repo.record(_s("sp-1", "gfs-a"))
    await repo.record(_s("sp-1", "gfs-b"))
    await repo.record(_s("sp-2", "gfs-a"))
    assert [s.gfs_instance_id for s in await repo.list_for_space("sp-1")] == [
        "gfs-a",
        "gfs-b",
    ]
    assert [s.space_id for s in await repo.list_for_gfs("gfs-a")] == ["sp-1", "sp-2"]
    assert await repo.list_for_space("sp-none") == []


async def test_re_recording_rebinds_connection_key_and_url(repo):
    await repo.record(_s("sp-1", "gfs-a"))
    assert await repo.get("sp-1", "gfs-a") == _s("sp-1", "gfs-a")
    rebound = _s("sp-1", "gfs-a", conn="c-2", key="pk-2", url="https://h.test")
    await repo.record(rebound)
    assert await repo.get("sp-1", "gfs-a") == rebound
    assert await repo.get("sp-1", "gfs-zzz") is None


async def test_forget_drops_only_that_seat(repo):
    await repo.record(_s("sp-1", "gfs-a"))
    await repo.record(_s("sp-1", "gfs-b"))
    await repo.forget("sp-1", "gfs-a")
    await repo.forget("sp-1", "gfs-zzz")  # absent → no-op
    assert [s.gfs_instance_id for s in await repo.list_for_space("sp-1")] == ["gfs-b"]


async def test_the_unmatched_clock_starts_once_resets_and_purges(repo, db):
    await repo.record(_s("sp-1", "gfs-a"))
    await repo.record(_s("sp-2", "gfs-b"))
    await repo.set_unmatched("sp-1", "gfs-a", unmatched=True)
    first = (await repo.get("sp-1", "gfs-a")).unmatched_since
    assert first is not None
    await repo.set_unmatched("sp-1", "gfs-a", unmatched=True)
    assert (await repo.get("sp-1", "gfs-a")).unmatched_since == first
    assert await repo.purge_unmatched(older_than_days=90) == 0
    await db.enqueue(
        "UPDATE gfs_space_seats SET unmatched_since=datetime('now', '-91 days')"
        " WHERE space_id='sp-1'"
    )
    assert await repo.purge_unmatched(older_than_days=90) == 1
    assert [s.space_id for s in await repo.list_all()] == ["sp-2"]
    # Re-taking a seat stops its clock; so does an explicit clear.
    await repo.set_unmatched("sp-2", "gfs-b", unmatched=True)
    await repo.record(_s("sp-2", "gfs-b"))
    assert (await repo.get("sp-2", "gfs-b")).unmatched_since is None
    await repo.set_unmatched("sp-2", "gfs-b", unmatched=True)
    await repo.set_unmatched("sp-2", "gfs-b", unmatched=False)
    assert (await repo.get("sp-2", "gfs-b")).unmatched_since is None

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


async def test_detach_release_and_the_sweep_markers(repo):
    await repo.record(_s("sp-1", "gfs-a"))
    await repo.mark_released("sp-1", "gfs-a")  # not detached: no-op
    assert not (await repo.get("sp-1", "gfs-a")).released
    await repo.mark_detached("sp-1", "gfs-a", at="2026-10-10 00:00:00")
    await repo.mark_detached("sp-1", "gfs-a", at="2026-11-11 00:00:00")
    seat = await repo.get("sp-1", "gfs-a")
    assert seat.detached and seat.detached_at == "2026-10-10 00:00:00"
    await repo.mark_released("sp-1", "gfs-a")
    await repo.set_expiry_seen("sp-1", "gfs-a", "2027-01-01 00:00:00")
    await repo.mark_address_warned("sp-1", "gfs-a")
    seat = await repo.get("sp-1", "gfs-a")
    assert seat.released and seat.expiry_seen_at and seat.address_warned
    await repo.set_detached_at("sp-1", "gfs-a", "2026-12-12 00:00:00")
    seat = await repo.get("sp-1", "gfs-a")
    assert seat.detached_at == "2026-12-12 00:00:00" and seat.expiry_seen_at is None
    # Re-taking the seat makes it a live seat again.
    await repo.record(_s("sp-1", "gfs-a"))
    seat = await repo.get("sp-1", "gfs-a")
    assert (seat.detached, seat.detached_at, seat.released, seat.address_warned) == (
        False,
        None,
        False,
        False,
    )
    assert [x.space_id for x in await repo.list_all()] == ["sp-1"]

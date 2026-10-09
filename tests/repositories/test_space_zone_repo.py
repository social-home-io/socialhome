"""Tests for :class:`SqliteSpaceZoneRepo`."""

from __future__ import annotations

import pytest

from socialhome.domain.space import SpaceZone
from socialhome.repositories.space_zone_repo import SqliteSpaceZoneRepo


@pytest.fixture
async def repo(db):
    """Two spaces, each owning one zone."""
    for sid in ("sp-a", "sp-b"):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?,?,?,?,?)",
            (sid, sid, "inst-x", "alice", "aa" * 32),
        )
    r = SqliteSpaceZoneRepo(db)
    assert await r.upsert(_zone("z-a", "sp-a"), space_id="sp-a")
    assert await r.upsert(_zone("z-b", "sp-b"), space_id="sp-b")
    return r


def _zone(zone_id: str, space_id: str, *, name: str = "Home") -> SpaceZone:
    return SpaceZone(
        id=zone_id,
        space_id=space_id,
        name=name,
        latitude=47.1234,
        longitude=8.5678,
        radius_m=150,
        color=None,
        created_by="uid-alice",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


async def test_upsert_and_list(repo):
    zones = await repo.list_for_space("sp-a")
    assert [z.id for z in zones] == ["z-a"]
    assert await repo.count_for_space("sp-b") == 1
    assert (await repo.get_by_name("sp-a", "Home")).id == "z-a"


async def test_upsert_updates_a_zone_of_the_same_space(repo):
    assert await repo.upsert(_zone("z-a", "sp-a", name="Office"), space_id="sp-a")
    assert (await repo.get("z-a")).name == "Office"


async def test_upsert_refuses_a_zone_id_of_another_space(repo):
    """A write gated for sp-a cannot rewrite sp-b's zone by naming its id."""
    assert not await repo.upsert(_zone("z-b", "sp-a", name="Evil"), space_id="sp-a")
    zone = await repo.get("z-b")
    assert zone.space_id == "sp-b"
    assert zone.name == "Home"


async def test_upsert_uses_the_scope_not_the_dataclass_space(repo):
    """``space_id`` decides where a new row lands, not ``zone.space_id``."""
    assert await repo.upsert(_zone("z-new", "sp-b", name="Park"), space_id="sp-a")
    assert (await repo.get("z-new")).space_id == "sp-a"


async def test_delete_is_scoped(repo):
    assert not await repo.delete("z-b", space_id="sp-a")
    assert await repo.get("z-b") is not None
    assert await repo.delete("z-b", space_id="sp-b")
    assert await repo.get("z-b") is None


# ─── Tombstones (migration 0085, §25.6 ``space_zones_deleted``) ──────


async def _row(repo, zone_id):
    row = await repo._db.fetchone("SELECT * FROM space_zones WHERE id=?", (zone_id,))
    return dict(row) if row is not None else None


async def test_a_zone_delete_keeps_a_tombstone_that_frees_its_name(repo):
    assert await repo.delete("z-a", space_id="sp-a", deleted_by="uid-adm")
    row = await _row(repo, "z-a")
    assert row is not None and row["deleted_at"] and row["deleted_by"] == "uid-adm"
    # The name and the coordinates are content: gone.
    assert row["name"] != "Home" and row["latitude"] == 0 and row["longitude"] == 0
    assert await repo.get("z-a") is None
    assert await repo.list_for_space("sp-a") == []
    assert await repo.count_for_space("sp-a") == 0
    assert await repo.get_by_name("sp-a", "Home") is None
    assert await repo.is_deleted("z-a", space_id="sp-a")
    assert not await repo.is_deleted("z-a", space_id="sp-b")
    assert not await repo.delete("z-a", space_id="sp-a")
    # The name is free for a new zone; the tombstoned id never comes back.
    assert await repo.upsert(_zone("z-a2", "sp-a"), space_id="sp-a")
    assert not await repo.upsert(_zone("z-a", "sp-a", name="Back"), space_id="sp-a")
    assert (await _row(repo, "z-a"))["deleted_at"] is not None


async def test_zone_tombstones_page_per_space(repo):
    for zid in ("z-1", "z-2", "z-3"):
        await repo.upsert(_zone(zid, "sp-a", name=zid), space_id="sp-a")
        await repo.delete(zid, space_id="sp-a")
    page, cursor = await repo.list_tombstones_page("sp-a", limit=2)
    assert [t.id for t in page] == ["z-1", "z-2"] and cursor is not None
    rest, end = await repo.list_tombstones_page("sp-a", cursor=cursor, limit=2)
    assert [t.id for t in rest] == ["z-3"] and end is None
    assert rest[0].owner == "uid-alice" and rest[0].deleted_by == ""
    assert await repo.list_tombstones_page("sp-b") == ([], None)


async def test_zone_tombstones_page_since_a_stamp(repo):
    for zid in ("z-1", "z-2"):
        await repo.upsert(_zone(zid, "sp-a", name=zid), space_id="sp-a")
    await repo.delete("z-1", space_id="sp-a")
    row = await repo._db.fetchone("SELECT seq FROM sync_seq_counter WHERE id=1")
    mark = int(row["seq"])
    assert await repo.list_tombstones_page("sp-a", since=mark) == ([], None)
    await repo.delete("z-2", space_id="sp-a")
    page, _ = await repo.list_tombstones_page("sp-a", since=mark)
    assert [t.id for t in page] == ["z-2"]

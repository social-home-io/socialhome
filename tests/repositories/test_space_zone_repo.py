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

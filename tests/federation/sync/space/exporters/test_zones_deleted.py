"""Tests for socialhome.federation.sync.space.exporters.zones_deleted."""

from __future__ import annotations

import pytest

from socialhome.domain.space import SpaceZone
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
)
from socialhome.federation.sync.space.exporters import ZonesDeletedExporter
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE
from socialhome.repositories.space_zone_repo import SqliteSpaceZoneRepo


@pytest.fixture
async def repo(db):
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','h','anna','ab')"
    )
    return SqliteSpaceZoneRepo(db)


def _zone(zid: str) -> SpaceZone:
    return SpaceZone(
        id=zid,
        space_id="sp",
        name=f"Grandma {zid}",
        latitude=47.1234,
        longitude=8.5678,
        radius_m=100,
        color=None,
        created_by="u-adm",
        created_at="2026-01-01",
        updated_at="2026-01-01",
    )


async def test_streams_every_tombstone_without_name_or_coordinates(repo):
    await repo.upsert(_zone("z-gone"), space_id="sp")
    await repo.upsert(_zone("z-kept"), space_id="sp")
    await repo.delete("z-gone", space_id="sp", deleted_by="u-adm")
    records = await ZonesDeletedExporter(repo).list_records("sp")
    assert records == [
        {
            "id": "z-gone",
            "created_by": "u-adm",
            "created_at": "2026-01-01",
            "actor_user_id": "u-adm",
        }
    ]
    assert "47.1" not in repr(records) and "Grandma" not in repr(records)


async def test_pages_past_one_page(repo):
    for i in range(SYNC_PAGE_SIZE + 1):
        await repo.upsert(_zone(f"z-{i}"), space_id="sp")
        await repo.delete(f"z-{i}", space_id="sp")
    batches = [b async for b in ZonesDeletedExporter(repo).iter_batches("sp")]
    assert [len(b) for b in batches] == [SYNC_PAGE_SIZE, 1]


def test_ships_before_the_live_zones_as_a_removal():
    assert RESOURCE_ORDER.index("space_zones_deleted") < RESOURCE_ORDER.index(
        "space_zones"
    )
    assert "space_zones_deleted" in REMOVAL_RESOURCES

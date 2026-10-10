"""Direct SQLite tests for ``SqliteGfsConnectionRepo``.

Exercises the literal SQL + the ``0027_gfs_publication_status``
migration against the live (fully-migrated) ``db`` fixture: this is
the implementation the GFS publish path runs in production.
"""

from __future__ import annotations

import pytest

from socialhome.domain.federation import GfsConnection
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo


pytestmark = pytest.mark.asyncio


@pytest.fixture
def repo(db):
    return SqliteGfsConnectionRepo(db)


def _conn(gfs_id: str = "gfs-1") -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name="A GFS",
        public_key="ab" * 32,
        inbox_url="https://gfs.example/inbox",
        status="active",
        paired_at="2026-06-06 00:00:00",
    )


async def test_publish_space_persists_and_returns_status(repo):
    await repo.save(_conn())
    pub = await repo.publish_space("space-1", "gfs-1", "pending")
    assert pub.space_id == "space-1"
    assert pub.gfs_connection_id == "gfs-1"
    assert pub.status == "pending"
    assert pub.published_at  # DB default populated the row

    # Persisted, not just returned.
    listed = await repo.list_publications("gfs-1")
    assert [p.status for p in listed] == ["pending"]


async def test_publish_space_default_status_is_active(repo):
    await repo.save(_conn())
    pub = await repo.publish_space("space-1", "gfs-1")
    assert pub.status == "active"
    listed = await repo.list_publications("gfs-1")
    assert listed[0].status == "active"


async def test_republish_upserts_status(repo):
    await repo.save(_conn())
    first = await repo.publish_space("space-1", "gfs-1", "active")
    assert first.status == "active"

    second = await repo.publish_space("space-1", "gfs-1", "pending")
    assert second.status == "pending"

    listed = await repo.list_publications("gfs-1")
    assert len(listed) == 1
    assert listed[0].status == "pending"


async def test_list_publications_for_space_filters_by_space(repo):
    await repo.save(_conn("gfs-1"))
    await repo.save(_conn("gfs-2"))
    await repo.publish_space("space-1", "gfs-1", "pending")
    await repo.publish_space("space-1", "gfs-2", "active")
    await repo.publish_space("space-2", "gfs-1", "active")

    rows = await repo.list_publications_for_space("space-1")
    assert {(r.gfs_connection_id, r.status) for r in rows} == {
        ("gfs-1", "pending"),
        ("gfs-2", "active"),
    }
    assert all(r.space_id == "space-1" for r in rows)


async def test_list_publications_all_carries_status(repo):
    await repo.save(_conn())
    await repo.publish_space("space-1", "gfs-1", "pending")
    rows = await repo.list_publications_all()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def _conn_with(gfs_id: str, *, status: str, paired_at: str) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name="A GFS",
        public_key="ab" * 32,
        inbox_url="https://gfs.example/inbox",
        status=status,
        paired_at=paired_at,
    )


async def test_update_display_name_changes_the_row(repo):
    await repo.save(_conn("gfs-1"))
    await repo.update_display_name("gfs-1", "Renamed GFS")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.display_name == "Renamed GFS"


async def test_update_gfs_instance_id_rebinds_the_row(repo):
    await repo.save(_conn("gfs-1"))
    assert await repo.update_gfs_instance_id("gfs-1", "gfs-shared") is True
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.gfs_instance_id == "gfs-shared"


async def test_update_gfs_instance_id_refuses_an_id_another_connection_holds(repo):
    """``gfs_instance_id`` is UNIQUE: the same server paired twice keeps both
    rows untouched instead of failing the write."""
    await repo.save(_conn("gfs-1"))
    await repo.save(_conn("gfs-2"))
    assert await repo.update_gfs_instance_id("gfs-1", "inst-gfs-2") is False
    assert (await repo.get("gfs-1")).gfs_instance_id == "inst-gfs-1"
    assert (await repo.get("gfs-2")).gfs_instance_id == "inst-gfs-2"


async def test_update_gfs_instance_id_to_its_own_id_is_a_noop(repo):
    await repo.save(_conn("gfs-1"))
    assert await repo.update_gfs_instance_id("gfs-1", "inst-gfs-1") is True
    assert (await repo.get("gfs-1")).gfs_instance_id == "inst-gfs-1"


async def test_list_all_returns_every_status_ordered_by_paired_at_desc(repo):
    await repo.save(
        _conn_with("g-active", status="active", paired_at="2026-06-01 00:00:00")
    )
    await repo.save(
        _conn_with("g-pending", status="pending", paired_at="2026-06-03 00:00:00")
    )
    await repo.save(
        _conn_with("g-suspended", status="suspended", paired_at="2026-06-02 00:00:00")
    )
    rows = await repo.list_all()
    assert [r.id for r in rows] == ["g-pending", "g-suspended", "g-active"]
    assert {r.status for r in rows} == {"active", "pending", "suspended"}


async def test_list_active_still_filters_to_active_only(repo):
    await repo.save(
        _conn_with("g-active", status="active", paired_at="2026-06-01 00:00:00")
    )
    await repo.save(
        _conn_with("g-pending", status="pending", paired_at="2026-06-03 00:00:00")
    )
    await repo.save(
        _conn_with("g-suspended", status="suspended", paired_at="2026-06-02 00:00:00")
    )
    active = await repo.list_active()
    assert [r.id for r in active] == ["g-active"]

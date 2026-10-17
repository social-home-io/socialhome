"""Direct SQLite tests for ``SqliteGfsConnectionRepo``.

Exercises the literal SQL + the ``0027_gfs_publication_status``
migration against the live (fully-migrated) ``db`` fixture: this is
the implementation the GFS publish path runs in production.
"""

from __future__ import annotations

from dataclasses import replace

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


async def test_list_active_listings_for_space(repo):
    """Only ACTIVE publications on ACTIVE connections, ordered by the GFS's
    instance id (a stable order, so an unchanged list compares equal)."""
    for gid in ("gfs-1", "gfs-2", "gfs-3", "gfs-4"):
        await repo.save(_conn(gid))
    await repo.update_status("gfs-4", "suspended")
    await repo.publish_space("space-1", "gfs-2", "active")
    await repo.publish_space("space-1", "gfs-1", "active")
    await repo.publish_space("space-1", "gfs-3", "pending")
    await repo.publish_space("space-1", "gfs-4", "active")
    await repo.publish_space("space-2", "gfs-1", "active")

    rows = await repo.list_active_listings_for_space("space-1")
    assert [c.id for c in rows] == ["gfs-1", "gfs-2"]
    assert await repo.list_active_listings_for_space("space-9") == []


async def test_proof_required_starts_unset_and_ratchets(repo):
    await repo.save(_conn("gfs-1"))
    assert (await repo.get("gfs-1")).proof_required_at is None
    assert await repo.mark_proof_required("gfs-1", "2026-10-10T00:00:00+00:00")
    assert (await repo.get("gfs-1")).proof_required_at == "2026-10-10T00:00:00+00:00"
    # Never moves once set — the first proof is what is remembered.
    assert not await repo.mark_proof_required("gfs-1", "2027-01-01T00:00:00+00:00")
    assert (await repo.get("gfs-1")).proof_required_at == "2026-10-10T00:00:00+00:00"


async def test_save_never_clears_the_proof_ratchet(repo):
    await repo.save(_conn("gfs-1"))
    await repo.mark_proof_required("gfs-1", "2026-10-10T00:00:00+00:00")
    # A later save of an object read before the stamp (None) keeps it.
    await repo.save(_conn("gfs-1"))
    assert (await repo.get("gfs-1")).proof_required_at == "2026-10-10T00:00:00+00:00"


async def test_save_persists_a_proof_stamp(repo):
    await repo.save(
        replace(_conn("gfs-1"), proof_required_at="2026-10-10T01:00:00+00:00")
    )
    assert (await repo.get("gfs-1")).proof_required_at == "2026-10-10T01:00:00+00:00"


async def test_channel_withdrawals_round_trip(repo):
    await repo.save(_conn("gfs-1"))
    await repo.save(_conn("gfs-2"))
    ch = "ab" * 16
    await repo.add_channel_withdrawals("sp", ch, ["gfs-1", "gfs-2", "gfs-1", "nope"])
    await repo.add_channel_withdrawals("sp-2", ch, ["gfs-2"])
    assert await repo.list_channel_withdrawals(space_id="sp") == [
        ("sp", "gfs-1", ch),
        ("sp", "gfs-2", ch),
    ]
    assert await repo.list_channel_withdrawals(gfs_id="gfs-2") == [
        ("sp", "gfs-2", ch),
        ("sp-2", "gfs-2", ch),
    ]
    assert await repo.list_channel_withdrawals(space_id="sp", gfs_id="gfs-1") == [
        ("sp", "gfs-1", ch)
    ]
    await repo.delete_channel_withdrawal("sp", "gfs-1", ch)
    assert len(await repo.list_channel_withdrawals()) == 2
    # Disconnecting a GFS drops its pending withdrawals.
    await repo.delete("gfs-2")
    assert await repo.list_channel_withdrawals() == []

"""Direct SQLite tests for ``SqliteGfsConnectionRepo``.

Exercises the literal SQL + the ``0027_gfs_publication_status``
migration against the live (fully-migrated) ``db`` fixture: this is
the implementation the GFS publish path runs in production.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from socialhome.domain.federation import GfsConnection
from socialhome.infrastructure.key_manager import KeyManager, KeyManagerError
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


async def test_transition_status_is_compare_and_set(repo):
    """Moves the row only out of an expected status, and says whether it
    did — two callers racing on one transition: exactly one wins."""
    await repo.save(
        _conn_with("g-1", status="pending", paired_at="2026-06-01 00:00:00")
    )
    assert await repo.transition_status("g-1", "active", ("pending", "suspended"))
    assert (await repo.get("g-1")).status == "active"
    # Already active: no second transition.
    assert not await repo.transition_status("g-1", "active", ("pending", "suspended"))
    assert not await repo.transition_status("g-1", "suspended", ("pending",))
    assert (await repo.get("g-1")).status == "active"
    # A removed connection is never brought back.
    assert not await repo.transition_status("gone", "active", ("pending",))
    assert await repo.get("gone") is None


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


# ── Withdrawn listings that still hold a pin (migration 0147) ────────────

_KEY_1 = "11" * 32
_KEY_2 = "22" * 32


async def test_withdrawn_listings_round_trip(repo):
    await repo.save(_conn("gfs-1"))
    await repo.save(_conn("gfs-2"))
    await repo.mark_listing_withdrawn("sp", "gfs-1", _KEY_1)
    await repo.mark_listing_withdrawn("sp", "gfs-2", None)
    await repo.mark_listing_withdrawn("sp-2", "gfs-2", _KEY_2)
    # A connection that is gone records nothing.
    await repo.mark_listing_withdrawn("sp", "nope", _KEY_1)
    assert await repo.list_withdrawn_listings(space_id="sp") == [
        ("sp", "gfs-1", _KEY_1),
        ("sp", "gfs-2", None),
    ]
    assert await repo.list_withdrawn_listings(gfs_id="gfs-2") == [
        ("sp", "gfs-2", None),
        ("sp-2", "gfs-2", _KEY_2),
    ]
    await repo.set_withdrawn_listing_pin("sp", "gfs-2", _KEY_2)
    assert await repo.list_withdrawn_listings(space_id="sp", gfs_id="gfs-2") == [
        ("sp", "gfs-2", _KEY_2)
    ]
    await repo.delete_withdrawn_listing("sp", "gfs-1")
    assert len(await repo.list_withdrawn_listings()) == 2
    # Disconnecting a GFS drops what it still held.
    await repo.delete("gfs-2")
    assert await repo.list_withdrawn_listings() == []


async def test_marking_a_withdrawn_listing_again_keeps_a_known_pin(repo):
    await repo.save(_conn("gfs-1"))
    await repo.mark_listing_withdrawn("sp", "gfs-1", _KEY_1)
    # A second withdrawal that knows nothing must not forget the pin.
    await repo.mark_listing_withdrawn("sp", "gfs-1", None)
    assert await repo.list_withdrawn_listings() == [("sp", "gfs-1", _KEY_1)]
    await repo.mark_listing_withdrawn("sp", "gfs-1", _KEY_2)
    assert await repo.list_withdrawn_listings() == [("sp", "gfs-1", _KEY_2)]


async def test_listing_a_space_again_ends_its_withdrawn_row(repo):
    await repo.save(_conn("gfs-1"))
    await repo.mark_listing_withdrawn("sp", "gfs-1", _KEY_1)
    await repo.publish_space("sp", "gfs-1")
    assert await repo.list_withdrawn_listings() == []


# ── A retired channel's wrapped signing seed (migration 0147) ────────────


async def test_channel_withdrawal_keeps_its_seed_wrapped(db):
    repo = SqliteGfsConnectionRepo(db, key_manager=KeyManager(b"\x07" * 32))
    await repo.save(_conn("gfs-1"))
    await repo.save(_conn("gfs-2"))
    ch = "ab" * 16
    seed = b"\x05" * 32
    await repo.add_channel_withdrawals("sp", ch, ["gfs-1"])
    assert await repo.get_channel_withdrawal_seed("sp", "gfs-1", ch) is None
    # Recorded again WITH the seed: new rows carry it, and the row that
    # had none gains it.
    await repo.add_channel_withdrawals("sp", ch, ["gfs-1", "gfs-2"], channel_seed=seed)
    assert await repo.get_channel_withdrawal_seed("sp", "gfs-1", ch) == seed
    assert await repo.get_channel_withdrawal_seed("sp", "gfs-2", ch) == seed
    assert await repo.list_channel_withdrawals(space_id="sp") == [
        ("sp", "gfs-1", ch),
        ("sp", "gfs-2", ch),
    ]
    # At rest it is ciphertext, bound to its row.
    row = await db.fetchone(
        "SELECT channel_seed FROM gfs_channel_withdrawals WHERE gfs_connection_id='gfs-1'"
    )
    assert seed.hex() not in row["channel_seed"]
    await db.enqueue(
        "UPDATE gfs_channel_withdrawals SET channel_seed=("
        " SELECT channel_seed FROM gfs_channel_withdrawals"
        " WHERE gfs_connection_id='gfs-1') WHERE gfs_connection_id='gfs-2'"
    )
    # A value that does not unwrap for its row is an error, never "no seed".
    with pytest.raises(KeyManagerError):
        await repo.get_channel_withdrawal_seed("sp", "gfs-2", ch)
    assert await repo.get_channel_withdrawal_seed("sp", "nope", ch) is None


async def test_channel_seed_needs_the_kek(repo):
    await repo.save(_conn("gfs-1"))
    with pytest.raises(RuntimeError):
        await repo.add_channel_withdrawals(
            "sp", "ab" * 16, ["gfs-1"], channel_seed=b"\x05" * 32
        )
    await repo.add_channel_withdrawals("sp", "ab" * 16, ["gfs-1"])
    assert await repo.get_channel_withdrawal_seed("sp", "gfs-1", "ab" * 16) is None
    # A stored seed with no KEK to read it is an error too.
    await repo._db.enqueue("UPDATE gfs_channel_withdrawals SET channel_seed='x'")
    with pytest.raises(KeyManagerError):
        await repo.get_channel_withdrawal_seed("sp", "gfs-1", "ab" * 16)

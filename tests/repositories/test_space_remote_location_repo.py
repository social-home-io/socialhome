"""SqliteSpaceRemoteLocationRepo — remote members' pins on a space map."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.repositories.space_remote_location_repo import (
    SpaceRemoteLocation,
    SqliteSpaceRemoteLocationRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await SqliteSpaceRepo(db).save(
        Space(
            id="sp-1",
            name="S",
            owner_instance_id="host.home",
            owner_username="ann",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    yield SqliteSpaceRemoteLocationRepo(db)
    await db.shutdown()


def _pin(lat: float, updated_at: str | None) -> SpaceRemoteLocation:
    return SpaceRemoteLocation(
        space_id="sp-1",
        instance_id="peer.home",
        user_id="u1",
        mode="gps",
        latitude=lat,
        longitude=8.5,
        updated_at=updated_at,
    )


async def test_a_newer_pin_replaces_an_older_one(repo):
    await repo.upsert(_pin(47.0, "2026-10-22T09:00:00+00:00"))
    await repo.upsert(_pin(47.1, "2026-10-22T10:00:00+00:00"))
    [row] = await repo.list_for_space("sp-1")
    assert row.latitude == 47.1


async def test_an_older_pin_that_arrives_late_changes_nothing(repo):
    """#1117 residual: a pin that took a slower path (an outbox retry, the
    mesh) used to overwrite the newer one already on the map."""
    await repo.upsert(_pin(47.1, "2026-10-22T10:00:00+00:00"))
    await repo.upsert(_pin(47.0, "2026-10-22T09:00:00+00:00"))
    [row] = await repo.list_for_space("sp-1")
    assert row.latitude == 47.1
    assert row.updated_at == "2026-10-22T10:00:00+00:00"


async def test_the_same_pin_again_is_taken(repo):
    await repo.upsert(_pin(47.0, "2026-10-22T09:00:00+00:00"))
    await repo.upsert(_pin(47.2, "2026-10-22T09:00:00+00:00"))
    [row] = await repo.list_for_space("sp-1")
    assert row.latitude == 47.2


async def test_instants_compare_across_formats(repo):
    """A row stamped by the database (naive, UTC) and a wire stamp with an
    offset order as instants, not as strings."""
    await repo.upsert(_pin(47.0, "2026-10-22 09:30:00"))
    await repo.upsert(_pin(47.1, "2026-10-22T11:00:00+02:00"))  # 09:00 UTC
    [row] = await repo.list_for_space("sp-1")
    assert row.latitude == 47.0
    await repo.upsert(_pin(47.2, "2026-10-22T09:45:00+00:00"))
    [row] = await repo.list_for_space("sp-1")
    assert row.latitude == 47.2


async def test_delete_for_member_removes_the_pin(repo):
    await repo.upsert(_pin(47.0, None))
    await repo.delete_for_member("sp-1", "peer.home", "u1")
    assert await repo.list_for_space("sp-1") == []

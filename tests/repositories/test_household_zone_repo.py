"""Tests for :class:`SqliteHouseholdZoneRepo` (real SQLite via the ``db``
fixture)."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.domain.household_zone import HouseholdZone
from socialhome.repositories.household_zone_repo import (
    AbstractHouseholdZoneRepo,
    SqliteHouseholdZoneRepo,
)


def _zone(
    zid: str,
    name: str,
    *,
    is_home: bool = False,
    external_id: str | None = None,
    lat: float = 52.52,
    icon: str | None = None,
) -> HouseholdZone:
    return HouseholdZone(
        id=zid,
        name=name,
        latitude=lat,
        longitude=13.405,
        radius_m=150,
        color="#3b82f6",
        icon=icon,
        is_home=is_home,
        external_id=external_id,
        created_at="2026-10-13T00:00:00+00:00",
        updated_at="2026-10-13T00:00:00+00:00",
    )


@pytest.fixture
async def repo(db):
    return SqliteHouseholdZoneRepo(db)


def test_satisfies_protocol(repo):
    assert isinstance(repo, AbstractHouseholdZoneRepo)


async def test_create_get_roundtrip(repo):
    z = _zone("hz_1", "School", icon="🏫")
    await repo.create(z)
    assert await repo.get("hz_1") == z
    assert await repo.get("missing") is None


async def test_list_home_first_then_name(repo):
    await repo.create(_zone("hz_w", "work"))
    await repo.create(_zone("hz_s", "School"))
    await repo.create(_zone("hz_h", "Zuhause", is_home=True))
    await repo.create(_zone("hz_a", "Aunt"))
    names = [z.name for z in await repo.list()]
    assert names == ["Zuhause", "Aunt", "School", "work"]
    assert await repo.count() == 4


async def test_get_by_name_is_case_insensitive(repo):
    await repo.create(_zone("hz_1", "School"))
    assert (await repo.get_by_name("school")).id == "hz_1"
    assert await repo.get_by_name("Work") is None


async def test_get_by_external_id(repo):
    await repo.create(_zone("hz_1", "Home", external_id="zone.home"))
    assert (await repo.get_by_external_id("zone.home")).id == "hz_1"
    assert await repo.get_by_external_id("zone.other") is None


async def test_create_duplicate_name_raises(repo):
    await repo.create(_zone("hz_1", "School"))
    with pytest.raises(sqlite3.IntegrityError):
        await repo.create(_zone("hz_2", "SCHOOL"))


async def test_create_home_moves_the_flag(repo):
    await repo.create(_zone("hz_1", "Old home", is_home=True))
    await repo.create(_zone("hz_2", "New home", is_home=True))
    assert (await repo.get("hz_1")).is_home is False
    assert (await repo.get("hz_2")).is_home is True


async def test_update_rewrites_fields_and_moves_home(repo):
    await repo.create(_zone("hz_1", "Home", is_home=True))
    await repo.create(_zone("hz_2", "School"))
    changed = _zone("hz_2", "Flat", is_home=True, lat=48.1, icon="🏠")
    assert await repo.update(changed) is True
    assert await repo.get("hz_2") == changed
    assert (await repo.get("hz_1")).is_home is False


async def test_update_missing_returns_false(repo):
    assert await repo.update(_zone("nope", "X")) is False
    assert await repo.count() == 0


async def test_update_missing_home_keeps_existing_home(repo):
    """A no-op update of an unknown id must not strip the current Home."""
    await repo.create(_zone("hz_1", "Home", is_home=True))
    assert await repo.update(_zone("nope", "X", is_home=True)) is False
    assert (await repo.get("hz_1")).is_home is True


async def test_delete(repo):
    await repo.create(_zone("hz_1", "School"))
    assert await repo.delete("hz_1") is True
    assert await repo.delete("hz_1") is False
    assert await repo.get("hz_1") is None


async def test_mirror_external_inserts_updates_and_removes(repo):
    await repo.create(_zone("hz_native", "School"))
    await repo.create(_zone("hz_x1", "Gym", external_id="zone.gym"))
    await repo.create(_zone("hz_x2", "Old", external_id="zone.old"))

    upserted, removed = await repo.mirror_external(
        [
            _zone("hz_new_gym", "Gym 2", external_id="zone.gym", icon="🏋"),
            _zone("hz_x3", "Work", external_id="zone.work"),
        ]
    )
    assert (upserted, removed) == (2, 1)

    gym = await repo.get_by_external_id("zone.gym")
    # The existing row keeps its id and created_at; the fields follow.
    assert gym.id == "hz_x1"
    assert gym.name == "Gym 2" and gym.icon == "🏋"
    assert await repo.get_by_external_id("zone.old") is None
    assert (await repo.get_by_external_id("zone.work")).id == "hz_x3"
    # Native rows are never touched.
    assert (await repo.get("hz_native")).name == "School"


async def test_mirror_external_swapped_names_do_not_collide(repo):
    await repo.create(_zone("hz_a", "A", external_id="zone.a"))
    await repo.create(_zone("hz_b", "B", external_id="zone.b"))
    await repo.mirror_external(
        [
            _zone("x", "B", external_id="zone.a"),
            _zone("y", "A", external_id="zone.b"),
        ]
    )
    assert (await repo.get("hz_a")).name == "B"
    assert (await repo.get("hz_b")).name == "A"


async def test_mirror_external_home_takes_the_flag(repo):
    await repo.create(_zone("hz_native", "My home", is_home=True))
    await repo.mirror_external(
        [_zone("hz_ha", "Home", is_home=True, external_id="zone.home")]
    )
    assert (await repo.get("hz_native")).is_home is False
    assert (await repo.get_by_external_id("zone.home")).is_home is True


async def test_mirror_external_without_home_keeps_native_home(repo):
    await repo.create(_zone("hz_native", "My home", is_home=True))
    await repo.mirror_external([_zone("hz_ha", "Work", external_id="zone.work")])
    assert (await repo.get("hz_native")).is_home is True


async def test_mirror_external_empty_removes_all_external(repo):
    await repo.create(_zone("hz_native", "School"))
    await repo.create(_zone("hz_x1", "Gym", external_id="zone.gym"))
    assert await repo.mirror_external([]) == (0, 1)
    assert [z.id for z in await repo.list()] == ["hz_native"]


async def test_mirror_external_is_atomic(repo):
    """A failing row rolls the whole mirror back."""
    await repo.create(_zone("hz_native", "School"))
    await repo.create(_zone("hz_x1", "Gym", external_id="zone.gym"))
    with pytest.raises(sqlite3.IntegrityError):
        await repo.mirror_external([_zone("hz_x2", "school", external_id="zone.s")])
    assert (await repo.get_by_external_id("zone.gym")).name == "Gym"
    assert await repo.count() == 2

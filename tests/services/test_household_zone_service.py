"""Tests for :class:`HouseholdZoneService` (in-memory repo stub)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from socialhome.domain.events import HouseholdZonesChanged
from socialhome.domain.household_zone import (
    MAX_HOUSEHOLD_ZONES,
    ExternalHouseholdZone,
    HouseholdZone,
    HouseholdZoneIsHomeError,
    HouseholdZoneLimitError,
    HouseholdZoneNameTakenError,
    HouseholdZoneNotFoundError,
    HouseholdZoneReadOnlyError,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.household_zone_service import HouseholdZoneService


class _FakeRepo:
    """Dict-backed stand-in mirroring the SQLite repo's semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, HouseholdZone] = {}

    async def list(self) -> list[HouseholdZone]:
        return sorted(
            self.rows.values(), key=lambda z: (not z.is_home, z.name.lower(), z.id)
        )

    async def get(self, zone_id):
        return self.rows.get(zone_id)

    async def get_by_name(self, name):
        return next(
            (z for z in self.rows.values() if z.name.lower() == name.lower()), None
        )

    async def get_by_external_id(self, external_id):
        return next(
            (z for z in self.rows.values() if z.external_id == external_id), None
        )

    async def count(self):
        return len(self.rows)

    def _clear_home(self, except_id):
        for k, z in list(self.rows.items()):
            if z.is_home and k != except_id:
                self.rows[k] = replace(z, is_home=False)

    async def create(self, zone):
        if zone.is_home:
            self._clear_home(zone.id)
        self.rows[zone.id] = zone

    async def update(self, zone):
        old = self.rows.get(zone.id)
        if old is None:
            return False
        if zone.is_home:
            self._clear_home(zone.id)
        self.rows[zone.id] = replace(
            zone, created_at=old.created_at, external_id=old.external_id
        )
        return True

    async def delete(self, zone_id):
        return self.rows.pop(zone_id, None) is not None

    async def mirror_external(self, zones: Sequence[HouseholdZone]):
        keep = {z.external_id for z in zones}
        removed = 0
        for k, z in list(self.rows.items()):
            if z.external_id is not None and z.external_id not in keep:
                del self.rows[k]
                removed += 1
        if any(z.is_home for z in zones):
            self._clear_home("")
        for z in zones:
            old = await self.get_by_external_id(z.external_id)
            if old is not None:
                self.rows[old.id] = replace(z, id=old.id, created_at=old.created_at)
            else:
                self.rows[z.id] = z
        return len(zones), removed


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def events(bus):
    seen: list = []

    async def _on(event):
        seen.append(event)

    bus.subscribe(HouseholdZonesChanged, _on)
    return seen


@pytest.fixture
def repo():
    return _FakeRepo()


@pytest.fixture
def svc(repo, bus):
    return HouseholdZoneService(repo, bus=bus)


async def _create(svc, name="School", **kw):
    args = dict(latitude=52.52, longitude=13.405, radius_m=150)
    args.update(kw)
    return await svc.create_zone(actor_is_admin=True, name=name, **args)


def _ext(eid, name, **kw) -> ExternalHouseholdZone:
    args = dict(latitude=52.52, longitude=13.405, radius_m=100)
    args.update(kw)
    return ExternalHouseholdZone(external_id=eid, name=name, **args)


# ─── list / create ───────────────────────────────────────────────────────


async def test_create_validates_and_truncates(svc, events):
    z = await _create(
        svc,
        name="  School  ",
        latitude=52.520049,
        longitude=13.404951,
        color="#AABBCC",
        icon="🏫",
    )
    assert z.id.startswith("hz_")
    assert z.name == "School"
    assert (z.latitude, z.longitude) == (52.52, 13.405)
    assert z.color == "#aabbcc" and z.icon == "🏫"
    assert z.is_home is False and z.external_id is None
    assert z.created_at.endswith("+00:00") and z.created_at == z.updated_at
    assert await svc.list_zones() == [z]
    assert len(events) == 1


async def test_create_requires_admin(svc, repo, events):
    with pytest.raises(PermissionError):
        await svc.create_zone(
            actor_is_admin=False, name="X", latitude=1, longitude=2, radius_m=100
        )
    assert repo.rows == {} and events == []


@pytest.mark.parametrize(
    "kw",
    [
        {"radius_m": 10},
        {"radius_m": 60_000},
        {"color": "red"},
        {"icon": "ab"},
        {"latitude": 91},
        {"longitude": float("nan")},
        {"name": ""},
        {"is_home": "yes"},
    ],
)
async def test_create_invalid_raises_value_error(svc, kw):
    with pytest.raises(ValueError):
        await _create(svc, **kw)


async def test_create_name_taken_any_case(svc):
    await _create(svc, name="School")
    with pytest.raises(HouseholdZoneNameTakenError):
        await _create(svc, name="school")


async def test_create_cap(svc, repo):
    for i in range(MAX_HOUSEHOLD_ZONES):
        await _create(svc, name=f"Z{i}")
    with pytest.raises(HouseholdZoneLimitError):
        await _create(svc, name="One too many")


async def test_create_home_moves_the_flag(svc):
    old = await _create(svc, name="Old", is_home=True)
    new = await _create(svc, name="New", is_home=True)
    zones = await svc.list_zones()
    assert zones[0].id == new.id and zones[0].is_home
    assert not next(z for z in zones if z.id == old.id).is_home


async def test_create_home_never_steals_an_external_home(svc, repo):
    await svc.mirror_external([_ext("zone.home", "Home", is_home=True)])
    with pytest.raises(HouseholdZoneReadOnlyError):
        await _create(svc, name="Mine", is_home=True)
    # A non-home native zone is still fine.
    await _create(svc, name="School")


# ─── update ──────────────────────────────────────────────────────────────


async def test_update_partial_and_tri_state(svc, events):
    z = await _create(svc, color="#112233", icon="🏫")
    events.clear()
    out = await svc.update_zone(z.id, actor_is_admin=True, radius_m=300, color=None)
    assert out.radius_m == 300
    assert out.color is None
    assert out.icon == "🏫"  # omitted → unchanged
    assert out.name == z.name and out.created_at == z.created_at
    assert len(events) == 1


async def test_update_requires_admin(svc):
    z = await _create(svc)
    with pytest.raises(PermissionError):
        await svc.update_zone(z.id, actor_is_admin=False, name="X")


async def test_update_unknown(svc):
    with pytest.raises(HouseholdZoneNotFoundError):
        await svc.update_zone("nope", actor_is_admin=True, name="X")


async def test_update_rename_clash(svc):
    await _create(svc, name="Work")
    z = await _create(svc, name="School")
    with pytest.raises(HouseholdZoneNameTakenError):
        await svc.update_zone(z.id, actor_is_admin=True, name="WORK")
    # Re-casing its own name is fine.
    out = await svc.update_zone(z.id, actor_is_admin=True, name="SCHOOL")
    assert out.name == "SCHOOL"


async def test_update_external_is_read_only(svc):
    await svc.mirror_external([_ext("zone.gym", "Gym")])
    gym = (await svc.list_zones())[0]
    with pytest.raises(HouseholdZoneReadOnlyError):
        await svc.update_zone(gym.id, actor_is_admin=True, name="X")


async def test_update_make_home_moves_flag(svc):
    home = await _create(svc, name="Home", is_home=True)
    flat = await _create(svc, name="Flat")
    out = await svc.update_zone(flat.id, actor_is_admin=True, is_home=True)
    assert out.is_home
    assert not (await svc.list_zones())[1].is_home
    assert (await svc.list_zones())[0].id == flat.id
    assert home.id != flat.id


async def test_update_cannot_unmark_home(svc):
    home = await _create(svc, name="Home", is_home=True)
    with pytest.raises(HouseholdZoneIsHomeError):
        await svc.update_zone(home.id, actor_is_admin=True, is_home=False)
    # Re-asserting is_home=True on the Home is a no-op, not an error.
    out = await svc.update_zone(home.id, actor_is_admin=True, is_home=True)
    assert out.is_home


async def test_update_make_home_never_steals_external_home(svc):
    await svc.mirror_external([_ext("zone.home", "Home", is_home=True)])
    z = await _create(svc, name="Flat")
    with pytest.raises(HouseholdZoneReadOnlyError):
        await svc.update_zone(z.id, actor_is_admin=True, is_home=True)


async def test_update_rejects_non_bool_is_home(svc):
    z = await _create(svc)
    with pytest.raises(ValueError):
        await svc.update_zone(z.id, actor_is_admin=True, is_home=1)


# ─── delete ──────────────────────────────────────────────────────────────


async def test_delete(svc, events):
    z = await _create(svc)
    events.clear()
    await svc.delete_zone(z.id, actor_is_admin=True)
    assert await svc.list_zones() == []
    assert len(events) == 1


async def test_delete_requires_admin(svc):
    z = await _create(svc)
    with pytest.raises(PermissionError):
        await svc.delete_zone(z.id, actor_is_admin=False)


async def test_delete_unknown(svc):
    with pytest.raises(HouseholdZoneNotFoundError):
        await svc.delete_zone("nope", actor_is_admin=True)


async def test_delete_home_refused(svc):
    home = await _create(svc, name="Home", is_home=True)
    with pytest.raises(HouseholdZoneIsHomeError):
        await svc.delete_zone(home.id, actor_is_admin=True)


async def test_delete_external_refused(svc):
    await svc.mirror_external([_ext("zone.gym", "Gym")])
    gym = (await svc.list_zones())[0]
    with pytest.raises(HouseholdZoneReadOnlyError):
        await svc.delete_zone(gym.id, actor_is_admin=True)


# ─── mirror_external ─────────────────────────────────────────────────────


async def test_mirror_external_upserts_and_removes(svc, events):
    native = await _create(svc, name="School")
    events.clear()
    r = await svc.mirror_external(
        [
            _ext("zone.home", "Home", is_home=True, latitude=52.123456),
            _ext("zone.work", "Work", icon="💼"),
        ]
    )
    assert (r.upserted, r.removed, r.skipped, r.changed) == (2, 0, 0, True)
    zones = await svc.list_zones()
    assert [z.name for z in zones] == ["Home", "School", "Work"]
    assert zones[0].is_home and zones[0].read_only
    assert zones[0].latitude == 52.1235  # 4 dp
    assert len(events) == 1

    r = await svc.mirror_external([_ext("zone.work", "Office")])
    assert (r.upserted, r.removed, r.changed) == (1, 1, True)
    assert [z.name for z in await svc.list_zones()] == ["Office", "School"]
    assert (await svc.list_zones())[1].id == native.id


async def test_mirror_external_unchanged_is_a_no_op(svc, repo, events):
    await svc.mirror_external([_ext("zone.work", "Work")])
    before = dict(repo.rows)
    events.clear()
    r = await svc.mirror_external([_ext("zone.work", "Work")])
    assert r.changed is False and r.upserted == 0 and r.removed == 0
    assert repo.rows == before
    assert events == []


async def test_mirror_external_skips_invalid_and_duplicates(svc):
    r = await svc.mirror_external(
        [
            _ext("zone.ok", "Ok"),
            _ext("zone.bad", "Bad", radius_m=5),
            _ext("zone.ok", "Ok again"),
            _ext("", "No id"),
        ]
    )
    assert (r.upserted, r.skipped) == (1, 3)
    assert [z.name for z in await svc.list_zones()] == ["Ok"]


async def test_mirror_external_disambiguates_names(svc):
    await _create(svc, name="Gym")
    await svc.mirror_external([_ext("zone.gym", "gym"), _ext("zone.gym2", "Gym")])
    names = {z.name for z in await svc.list_zones()}
    # Native names win; synced ones get the first free " (n)" suffix.
    assert names == {"Gym", "gym (2)", "Gym (3)"}


async def test_mirror_external_only_one_home(svc):
    await svc.mirror_external(
        [
            _ext("zone.a", "A", is_home=True),
            _ext("zone.b", "B", is_home=True),
        ]
    )
    homes = [z for z in await svc.list_zones() if z.is_home]
    assert [h.name for h in homes] == ["A"]


async def test_mirror_external_takes_home_from_native(svc):
    await _create(svc, name="Mine", is_home=True)
    await svc.mirror_external([_ext("zone.home", "Home", is_home=True)])
    zones = await svc.list_zones()
    assert zones[0].name == "Home" and zones[0].is_home
    assert not any(z.is_home for z in zones[1:])


async def test_mirror_external_respects_the_cap(svc):
    for i in range(MAX_HOUSEHOLD_ZONES - 2):
        await _create(svc, name=f"N{i}")
    r = await svc.mirror_external(
        [
            _ext("zone.a", "A"),
            _ext("zone.b", "B"),
            _ext("zone.home", "Home", is_home=True),
        ]
    )
    # Home is kept first; one external doesn't fit.
    assert (r.upserted, r.skipped) == (2, 1)
    assert await svc.count_zones() == MAX_HOUSEHOLD_ZONES
    assert (await svc.list_zones())[0].name == "Home"


# ─── match ───────────────────────────────────────────────────────────────


async def test_match_finds_zone(svc):
    await _create(svc, name="Home", is_home=True, radius_m=100)
    await _create(svc, name="School", latitude=52.53, radius_m=100)
    assert (await svc.match(52.5201, 13.405)).name == "Home"
    assert (await svc.match(52.5301, 13.405)).name == "School"
    assert await svc.match(10.0, 10.0) is None


async def test_match_without_zones(svc):
    assert await svc.match(52.52, 13.405) is None


def test_service_without_bus_constructs(repo):
    assert HouseholdZoneService(repo) is not None

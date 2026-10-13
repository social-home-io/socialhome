"""Tests for socialhome.platform.ha.zones — HA ``zone.*`` → household zones."""

from __future__ import annotations

import pytest

from socialhome.domain.household_zone import ZoneSourceUnavailableError
from socialhome.domain.space import validate_zone_icon
from socialhome.platform.adapter import ZoneSource
from socialhome.platform.ha.zones import (
    DEFAULT_ZONE_EMOJI,
    MDI_ZONE_EMOJI,
    HaZoneSource,
    icon_for_mdi,
    zones_from_states,
)


def _zone(entity_id: str, **attrs) -> dict:
    """A state object as ``GET /api/states`` returns it."""
    base = {
        "latitude": 47.3769,
        "longitude": 8.5417,
        "radius": 100.0,
        "passive": False,
        "persons": [],
        "editable": True,
    }
    base.update(attrs)
    return {
        "entity_id": entity_id,
        "state": "0",
        "attributes": base,
        "last_changed": "2026-10-13T08:00:00+00:00",
        "last_updated": "2026-10-13T08:00:00+00:00",
    }


HOME = _zone("zone.home", friendly_name="Vizeli", icon="mdi:home", radius=120.0)
WORK = _zone("zone.work", friendly_name="Work", icon="mdi:briefcase")
GYM = _zone("zone.gym", friendly_name="Gym", icon="mdi:dumbbell", passive=True)


# ── zones_from_states ────────────────────────────────────────────────────


def test_keeps_zones_and_maps_attributes():
    zones = zones_from_states(
        [{"entity_id": "person.pascal", "attributes": {}}, HOME, WORK]
    )
    assert [z.external_id for z in zones] == ["zone.home", "zone.work"]
    home, work = zones
    assert home.is_home and not work.is_home
    assert home.name == "Vizeli"
    assert (home.latitude, home.longitude, home.radius_m) == (47.3769, 8.5417, 120.0)
    assert home.icon == "🏠" and work.icon == "💼"
    # HA stores no zone colour — Social Home's palette applies.
    assert home.color is None and work.color is None


def test_passive_zones_are_skipped():
    assert [z.external_id for z in zones_from_states([HOME, GYM])] == ["zone.home"]


def test_passive_must_be_true_to_skip():
    """Only an explicit ``passive: true`` hides a zone."""
    zones = zones_from_states([_zone("zone.x", friendly_name="X", passive="yes")])
    assert len(zones) == 1


def test_name_falls_back_to_the_object_id():
    zones = zones_from_states(
        [_zone("zone.my_work"), _zone("zone.blank", friendly_name="   ")]
    )
    assert [z.name for z in zones] == ["my work", "blank"]


def test_malformed_entries_are_ignored_or_passed_through():
    """Non-dicts / non-zone ids are dropped; odd attribute values go through
    unvalidated (the mirror repairs or skips them)."""
    zones = zones_from_states(
        [
            "garbage",
            {"entity_id": 7},
            {"entity_id": "zone.bare"},
            {"entity_id": "zone.weird", "attributes": "nope"},
            _zone("zone.bad", friendly_name="Bad", latitude="north", radius=None),
        ]
    )
    assert [z.external_id for z in zones] == ["zone.bare", "zone.weird", "zone.bad"]
    bare = zones[0]
    assert bare.latitude is None and bare.radius_m is None
    assert bare.icon == DEFAULT_ZONE_EMOJI
    assert zones[2].latitude == "north"


def test_empty_states_is_no_zones():
    assert zones_from_states([]) == []


# ── icon_for_mdi ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "icon, emoji",
    [
        ("mdi:home", "🏠"),
        ("mdi:office-building", "🏢"),
        ("mdi:school", "🏫"),
        ("mdi:cart", "🛒"),
        ("mdi:pine-tree", "🌳"),
        ("mdi:hospital-building", "🏥"),
        ("mdi:soccer", "⚽"),
        ("mdi:airplane", "✈️"),
        ("mdi:human-child", "🧒"),
        ("mdi:dog", "🐕"),
        ("MDI:Home", DEFAULT_ZONE_EMOJI),  # the prefix is case-sensitive in HA
        ("mdi:HOME", "🏠"),
        ("mdi:unknown-thing", DEFAULT_ZONE_EMOJI),
        ("hass:home", DEFAULT_ZONE_EMOJI),
        (None, DEFAULT_ZONE_EMOJI),
        (42, DEFAULT_ZONE_EMOJI),
    ],
)
def test_icon_for_mdi(icon, emoji):
    assert icon_for_mdi(icon) == emoji


@pytest.mark.parametrize("emoji", sorted(set(MDI_ZONE_EMOJI.values())))
def test_every_mapped_emoji_is_a_valid_zone_icon(emoji):
    """The mirror drops an invalid icon — every mapped one must pass."""
    assert validate_zone_icon(emoji) == emoji


def test_default_emoji_is_a_valid_zone_icon():
    assert validate_zone_icon(DEFAULT_ZONE_EMOJI) == DEFAULT_ZONE_EMOJI


# ── HaZoneSource ─────────────────────────────────────────────────────────


class _Client:
    def __init__(self, states):
        self.states = states

    async def fetch_states(self):
        return self.states


class _Adapter:
    def __init__(self, client):
        self._c = client

    @property
    def _client(self):
        if self._c is None:
            raise RuntimeError("HaAdapter used before on_startup — no HaClient wired")
        return self._c


async def test_source_lists_zones():
    source = HaZoneSource(_Adapter(_Client([HOME, WORK, GYM])))
    assert isinstance(source, ZoneSource)
    assert [z.external_id for z in await source.list_zones()] == [
        "zone.home",
        "zone.work",
    ]


async def test_source_with_only_home_is_just_home():
    """Every user zone deleted in HA: only ``zone.home`` is left — a real
    answer, so the sync removes the other synced zones."""
    zones = await HaZoneSource(_Adapter(_Client([HOME]))).list_zones()
    assert [z.external_id for z in zones] == ["zone.home"]


@pytest.mark.parametrize(
    "states",
    [
        [],  # zone integration not loaded yet (HA starting) / recovery mode
        [WORK],  # zones mid-load, home not there yet
        [{"entity_id": "light.kitchen", "state": "on", "attributes": {}}],
    ],
)
async def test_source_without_zone_home_is_unavailable(states):
    """HA's zone integration always creates ``zone.home``
    (core ``zone/__init__.py`` ``async_setup``), and ``zone`` is a
    default integration — an answer without it is HA still starting,
    never "no zones", so the synced zones must not be wiped."""
    with pytest.raises(ZoneSourceUnavailableError, match="zone.home"):
        await HaZoneSource(_Adapter(_Client(states))).list_zones()


async def test_source_passive_home_counts_as_loaded():
    """A user-defined passive ``zone.home`` still proves the zones are
    loaded — it's left out, the others sync."""
    passive_home = _zone("zone.home", friendly_name="Home", passive=True)
    zones = await HaZoneSource(_Adapter(_Client([passive_home, WORK]))).list_zones()
    assert [z.external_id for z in zones] == ["zone.work"]


async def test_source_failed_fetch_raises_not_empty():
    """A failed ``/api/states`` must never read as "HA has no zones"."""
    with pytest.raises(ZoneSourceUnavailableError):
        await HaZoneSource(_Adapter(_Client(None))).list_zones()


async def test_source_before_startup_raises_unavailable():
    with pytest.raises(ZoneSourceUnavailableError, match="before on_startup"):
        await HaZoneSource(_Adapter(None)).list_zones()

"""Tests for :mod:`socialhome.domain.household_zone`."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.errors import CodedError
from socialhome.domain.household_zone import (
    HOME_ZONE_PRESENCE_NAME,
    MAX_HOUSEHOLD_ZONES,
    ExternalHouseholdZone,
    HouseholdZone,
    HouseholdZoneIsHomeError,
    HouseholdZoneLimitError,
    HouseholdZoneNameTakenError,
    HouseholdZoneNotFoundError,
    HouseholdZoneReadOnlyError,
)


def _zone(**kw) -> HouseholdZone:
    base = dict(
        id="hz_1",
        name="School",
        latitude=52.52,
        longitude=13.405,
        radius_m=150,
        created_at="2026-10-13T00:00:00+00:00",
        updated_at="2026-10-13T00:00:00+00:00",
    )
    base.update(kw)
    return HouseholdZone(**base)


def test_defaults_native_not_home():
    z = _zone()
    assert z.color is None and z.icon is None
    assert z.is_home is False
    assert z.external_id is None
    assert z.read_only is False


def test_external_zone_is_read_only():
    assert _zone(external_id="zone.school").read_only is True


def test_is_frozen_and_slotted():
    z = _zone()
    with pytest.raises(dataclasses.FrozenInstanceError):
        z.name = "x"  # type: ignore[misc]
    assert not hasattr(z, "__dict__")


def test_presence_name_home_is_home_constant():
    assert HOME_ZONE_PRESENCE_NAME == "home"
    assert _zone(name="Casa", is_home=True).presence_zone_name == "home"
    assert _zone(name="School").presence_zone_name == "School"


def test_cap_matches_space_zones():
    assert MAX_HOUSEHOLD_ZONES == 50


def test_external_zone_input_defaults():
    e = ExternalHouseholdZone(
        external_id="zone.home",
        name="Home",
        latitude=1.0,
        longitude=2.0,
        radius_m=100,
    )
    assert e.is_home is False and e.icon is None and e.color is None


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (HouseholdZoneNotFoundError(), 404, "NOT_FOUND"),
        (HouseholdZoneLimitError(), 409, "ZONE_LIMIT"),
        (HouseholdZoneNameTakenError(), 409, "ZONE_NAME_TAKEN"),
        (HouseholdZoneReadOnlyError(), 409, "ZONE_READ_ONLY"),
        (HouseholdZoneIsHomeError(), 409, "ZONE_IS_HOME"),
    ],
)
def test_errors_are_coded(exc, status, code):
    assert isinstance(exc, CodedError)
    assert exc.status == status
    assert exc.code == code
    assert exc.detail


def test_limit_error_carries_the_cap():
    assert HouseholdZoneLimitError().params == {"max": MAX_HOUSEHOLD_ZONES}


def test_not_found_is_a_lookup_error():
    assert isinstance(HouseholdZoneNotFoundError(), LookupError)

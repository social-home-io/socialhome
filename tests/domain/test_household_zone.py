"""Tests for :mod:`socialhome.domain.household_zone`."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.errors import CodedError
from socialhome.domain.household_zone import (
    HOME_ZONE_PRESENCE_NAME,
    MAX_HOUSEHOLD_ZONES,
    ZONE_PALETTE,
    ExternalHouseholdZone,
    HouseholdZone,
    HouseholdZoneIsHomeError,
    HouseholdZoneLimitError,
    HouseholdZoneMirrorResult,
    HouseholdZoneNameTakenError,
    HouseholdZoneNotFoundError,
    HouseholdZoneReadOnlyError,
    HouseholdZoneSyncStatus,
    ZoneSourceUnavailableError,
    zone_color,
)


def _zone(**kw) -> HouseholdZone:
    base = {
        "id": "hz_1",
        "name": "School",
        "latitude": 52.52,
        "longitude": 13.405,
        "radius_m": 150,
        "created_at": "2026-10-13T00:00:00+00:00",
        "updated_at": "2026-10-13T00:00:00+00:00",
    }
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


def test_mirror_result_shape():
    r = HouseholdZoneMirrorResult(upserted=2, removed=1, skipped=0, changed=True)
    assert (r.upserted, r.removed, r.skipped, r.changed) == (2, 1, 0, True)


def test_zone_source_unavailable_is_not_a_coded_api_error():
    """A failed platform read is an internal sync signal, never an HTTP
    error shape — the sync catches it and keeps the synced zones."""
    exc = ZoneSourceUnavailableError("HA down")
    assert not isinstance(exc, CodedError)
    assert str(exc) == "HA down"


def test_sync_status_defaults_and_frozen():
    status = HouseholdZoneSyncStatus(available=True, enabled=True)
    assert status.last_synced_at is None and status.last_error is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        status.enabled = False  # type: ignore[misc]


# ─── zone_color: the SPA's zoneColor, server-side ────────────────────────


def test_zone_color_keeps_a_valid_own_colour():
    assert zone_color(_zone(color="#10b981")) == "#10b981"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # Reference values from client/src/utils/zoneColor.ts (node).
        ("zone.home", "#84cc16"),
        ("zone.work", "#facc15"),
        ("zone.school", "#6366f1"),
        ("hz_abc", "#f97316"),
        ("hz_XyZ-_123", "#6366f1"),
        ("zone.grandma_s_house", "#ef4444"),
        ("ü😀x", "#84cc16"),
        ("a" * 40, "#10b981"),
    ],
)
def test_zone_color_hashes_like_the_spa(key, expected):
    """Same palette pick as the SPA, so the wall and the app agree —
    including the int32 wrap and an astral character's high surrogate."""
    assert zone_color(_zone(id=key)) == expected
    assert zone_color(_zone(id="hz_other", external_id=key)) == expected


def test_zone_color_prefers_the_external_id_and_refuses_bad_colours():
    synced = _zone(id="hz_new", external_id="zone.work")
    assert zone_color(synced) == zone_color(_zone(id="zone.work"))
    assert zone_color(_zone(id="hz_abc", color="red;x:y")) == "#f97316"
    assert zone_color(_zone(id="hz_abc", color="#fff")) == "#f97316"
    assert ZONE_PALETTE[0] == "#3b82f6" and len(ZONE_PALETTE) == 10

"""Integration tests for ``/api/household/zones/sync`` — real SQLite +
aiohttp ``TestClient``, with a fake :class:`ZoneSource` injected at the
platform boundary (the standalone adapter has none of its own)."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.app_keys import household_zone_sync_key
from socialhome.domain.household_zone import (
    ExternalHouseholdZone,
    ZoneSourceUnavailableError,
)
from socialhome.platform.standalone.adapter import StandaloneAdapter
from socialhome.services.household_zone_sync import SYNC_ENABLED_KEY

from .conftest import _auth
from .test_household_zones import _created, _member_token

_URL = "/api/household/zones/sync"
_ZONES = "/api/household/zones"


class _FakeHaZones:
    """Stands in for Home Assistant's zone.* entities."""

    def __init__(self) -> None:
        self.zones = [
            ExternalHouseholdZone(
                external_id="zone.home",
                name="Home",
                latitude=47.3769,
                longitude=8.5417,
                radius_m=100,
                is_home=True,
                icon="🏠",
            ),
            ExternalHouseholdZone(
                external_id="zone.work",
                name="Work",
                latitude=47.39,
                longitude=8.51,
                radius_m=150,
                icon="💼",
            ),
        ]
        self.fail = False

    async def list_zones(self):
        if self.fail:
            raise ZoneSourceUnavailableError("Home Assistant didn't answer")
        return list(self.zones)


@pytest.fixture
def ha_zones(monkeypatch):
    """Give the platform adapter a zone source — before ``client`` builds
    the app (requested first in ``synced``'s arguments)."""
    source = _FakeHaZones()
    monkeypatch.setattr(StandaloneAdapter, "zones", source, raising=False)
    return source


@pytest.fixture
async def synced(ha_zones, client):
    """A client whose startup sync has run."""
    for _ in range(200):
        r = await client.get(_ZONES, headers=_auth(client._tok))
        if len((await r.json())["zones"]) == 2:
            break
        await asyncio.sleep(0.01)
    return client


async def _zones(client) -> list[dict]:
    r = await client.get(_ZONES, headers=_auth(client._tok))
    assert r.status == 200
    return (await r.json())["zones"]


async def _status(client) -> dict:
    r = await client.get(_URL, headers=_auth(client._tok))
    assert r.status == 200, await r.text()
    return await r.json()


async def _patch(client, body, token=None):
    return await client.patch(_URL, json=body, headers=_auth(token or client._tok))


# ─── without a platform zone source ──────────────────────────────────────


async def test_standalone_is_unavailable(client):
    assert await _status(client) == {
        "available": False,
        "enabled": True,
        "last_synced_at": None,
        "last_error": None,
    }
    r = await _patch(client, {"enabled": False})
    assert r.status == 422


# ─── with Home Assistant's zones ─────────────────────────────────────────


async def test_startup_sync_mirrors_the_zones_read_only(synced):
    zones = await _zones(synced)
    assert [(z["external_id"], z["is_home"], z["read_only"]) for z in zones] == [
        ("zone.home", True, True),
        ("zone.work", False, True),
    ]
    status = await _status(synced)
    assert status["available"] and status["enabled"]
    assert status["last_synced_at"] is not None and status["last_error"] is None


async def test_synced_zone_is_read_only_through_the_api(synced):
    work = next(z for z in await _zones(synced) if z["external_id"] == "zone.work")
    r = await synced.patch(
        f"{_ZONES}/{work['id']}", json={"name": "Office"}, headers=_auth(synced._tok)
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "ZONE_READ_ONLY"


async def test_turning_off_removes_synced_keeps_native(synced):
    await _created(synced, "School")
    r = await _patch(synced, {"enabled": False})
    assert r.status == 200, await r.text()
    assert await r.json() == {
        "available": True,
        "enabled": False,
        "last_synced_at": None,
        "last_error": None,
    }
    assert [z["name"] for z in await _zones(synced)] == ["School"]
    row = await synced._db.fetchone(
        "SELECT value FROM instance_config WHERE key=?", (SYNC_ENABLED_KEY,)
    )
    assert row["value"] == "0"


async def test_turning_back_on_syncs_right_away(synced, ha_zones):
    await _patch(synced, {"enabled": False})
    ha_zones.zones = ha_zones.zones[1:]  # HA dropped its home zone meanwhile
    r = await _patch(synced, {"enabled": True})
    assert r.status == 200
    body = await r.json()
    assert body["enabled"] and body["last_synced_at"] is not None
    assert [z["external_id"] for z in await _zones(synced)] == ["zone.work"]


async def test_failed_read_keeps_synced_zones_and_reports(synced, ha_zones):
    ha_zones.fail = True
    r = await _patch(synced, {"enabled": True})
    assert r.status == 200
    assert (await r.json())["last_error"] == "Home Assistant didn't answer"
    assert len(await _zones(synced)) == 2


async def test_patch_validates_enabled(synced):
    r = await _patch(synced, {"enabled": "yes"})
    assert r.status == 422
    r = await synced.patch(_URL, json=[1], headers=_auth(synced._tok))
    assert r.status == 422


async def test_member_cannot_read_or_change_sync(synced):
    tok = await _member_token(synced)
    r = await synced.get(_URL, headers=_auth(tok))
    assert r.status == 403
    r = await _patch(synced, {"enabled": False}, token=tok)
    assert r.status == 403
    # Members still list the zones.
    r = await synced.get(_ZONES, headers=_auth(tok))
    assert r.status == 200


async def test_unauthenticated_is_401(synced):
    assert (await synced.get(_URL)).status == 401


async def test_feature_off_is_403(synced):
    r = await synced.put(
        "/api/household/preferences",
        json={"toggles": {"feat_presence": False}},
        headers=_auth(synced._tok),
    )
    assert r.status == 200
    r = await synced.get(_URL, headers=_auth(synced._tok))
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "FEATURE_DISABLED"


async def test_sync_route_wins_over_zone_id(synced):
    """``/sync`` isn't read as a zone id (registration order)."""
    r = await synced.delete(_URL, headers=_auth(synced._tok))
    assert r.status == 405


async def test_sync_service_is_wired(synced):
    assert synced.app[household_zone_sync_key].available

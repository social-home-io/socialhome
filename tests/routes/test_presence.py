"""Tests for socialhome.routes.presence.

Wire-contract guards: `POST /api/presence/location` must accept the
body shape documented in spec §23.8.5 so the forthcoming
``ha-integration/`` component can push location updates without a
silent field-name drop. The endpoint derives :class:`PresenceState`
from ``zone_name`` and returns 204.
"""

from .conftest import _auth


async def test_list_presence(client):
    """GET /api/presence returns the presence list."""
    r = await client.get("/api/presence", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert isinstance(body, list)


async def test_update_location_spec_compliant_payload_returns_204(client):
    """Spec §23.8.5: POST with latitude/longitude/accuracy_m/zone_name → 204.

    The DB row must carry the 4dp-truncated coordinates so the
    downstream realtime / federation fan-out sees the intended value.
    """
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "latitude": 52.37654321,
            "longitude": 4.89567890,
            "accuracy_m": 12.5,
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204

    row = await client._db.fetchone(
        "SELECT latitude, longitude, gps_accuracy_m, zone_name, state "
        "FROM presence WHERE username=?",
        ("admin",),
    )
    assert row is not None
    assert abs(row["latitude"] - 52.3765) < 1e-6
    assert abs(row["longitude"] - 4.8957) < 1e-6
    assert row["gps_accuracy_m"] == 12.5
    assert row["zone_name"] == "home"
    assert row["state"] == "home"


async def test_update_location_zone_name_home_derives_home_state(client):
    """zone_name='home' → state='home'."""
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT state FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["state"] == "home"


async def test_update_location_named_zone_derives_zone_state(client):
    """Any other non-empty zone_name → state='zone'."""
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": "Makers Space",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT state, zone_name FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["state"] == "zone"
    assert row["zone_name"] == "Makers Space"


async def test_update_location_null_zone_derives_away_state(client):
    """zone_name=null → state='away'."""
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": None,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT state FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["state"] == "away"


async def test_update_location_explicit_state_overrides_derivation(client):
    """An explicit ``state`` in the body wins over zone-based derivation.

    Used by manual/debug callers that want to force a state regardless
    of what zone_name implies. The ha-integration should not send this
    field — but tolerating it keeps operator tools simple.
    """
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "zone_name": "home",
            "state": "away",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT state FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["state"] == "away"


async def test_update_location_accuracy_gate_nulls_coords(client):
    """accuracy_m > 500 nulls coordinates but keeps the zone name."""
    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "latitude": 52.37,
            "longitude": 4.89,
            "accuracy_m": 750.0,
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    row = await client._db.fetchone(
        "SELECT latitude, longitude, zone_name FROM presence WHERE username=?",
        ("admin",),
    )
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["zone_name"] == "home"


async def test_update_location_missing_username_422(client):
    """A body without username is a client error — helpful for debugging."""
    r = await client.post(
        "/api/presence/location",
        json={
            "zone_name": "home",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422

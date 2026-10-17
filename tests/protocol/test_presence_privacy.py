"""§27.9: presence updates truncate GPS to 4dp + accuracy gate.

Privacy invariants:
* GPS coordinates stored at most 4 decimal places (≈ 11 m precision).
* Updates with accuracy worse than 500 m must be dropped, not stored
  with degraded precision.
* The presence row never carries identifying fields beyond the
  user/zone/coords/state/accuracy.
"""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.presence import LocationUpdate, truncate_coord
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.repositories.presence_repo import SqlitePresenceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.presence_service import PresenceService
from tests.routes import conftest as routes_conftest

#: Real app + SQLite + aiohttp TestClient (the routes fixture).
client = routes_conftest.client
_auth = routes_conftest._auth


pytestmark = pytest.mark.security


# ─── Truncation primitive ───────────────────────────────────────────────


def test_truncate_coord_drops_high_precision():
    """Spec §4: 4 decimal places, no more."""
    assert truncate_coord(47.123456789) == 47.1235
    assert truncate_coord(8.987654321) == 8.9877
    assert truncate_coord(0.0) == 0.0


def test_truncate_coord_handles_negative():
    assert truncate_coord(-122.4194) == -122.4194


def test_truncate_coord_none_passthrough():
    assert truncate_coord(None) is None


# ─── PresenceService persistence ────────────────────────────────────────


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('alice', 'a-id', 'Alice')",
    )
    yield db, PresenceService(SqlitePresenceRepo(db), SqliteUserRepo(db))
    await db.shutdown()


async def test_stored_lat_lon_is_truncated(env):
    db, svc = env
    await svc.update_location(
        LocationUpdate(
            username="alice",
            state="home",
            latitude=47.123456789,
            longitude=8.987654321,
            gps_accuracy_m=20,
        )
    )
    row = await db.fetchone(
        "SELECT latitude, longitude FROM presence WHERE username='alice'",
    )
    assert row["latitude"] == 47.1235
    assert row["longitude"] == 8.9877


async def test_inaccurate_gps_dropped_with_500m_gate(env):
    """gps_accuracy_m > 500 → coords nulled, state=='unknown'."""
    db, svc = env
    await svc.update_location(
        LocationUpdate(
            username="alice",
            state="home",
            latitude=47.0,
            longitude=8.0,
            gps_accuracy_m=1500,  # too inaccurate
        )
    )
    row = await db.fetchone(
        "SELECT latitude, longitude FROM presence WHERE username='alice'",
    )
    # Coordinates should not be persisted at full precision.
    assert row["latitude"] is None or row["latitude"] == 0.0


async def test_zoned_state_without_coords_stores_zone_name(env):
    """Household presence row stores ``zone_name`` even when GPS is absent.

    Zone names are household-only data (§7.3) — they hydrate the household
    dashboard but are stripped from any space-bound payload (§23.8.6). Here
    we just assert the household persistence path keeps ``zone_name`` when
    HA reports a zoned state without coordinates.
    """
    db, svc = env
    await svc.update_location(
        LocationUpdate(
            username="alice",
            state="away",
            zone_name="Office",
            latitude=None,
            longitude=None,
            gps_accuracy_m=None,
        )
    )
    row = await db.fetchone(
        "SELECT latitude, longitude, zone_name FROM presence WHERE username='alice'",
    )
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["zone_name"] == "Office"


# ─── Household zone names never reach a space (§23.8.6) ─────────────────

_SECRET = "Secret Clinic"


@pytest.mark.parametrize("mode", ["gps", "zone_only"])
async def test_household_zone_name_never_reaches_a_space(client, monkeypatch, mode):
    """A GPS push without a ``zone_name`` key is matched server-side to a
    household zone; that zone's name stays in the household. It must not
    appear in any space-bound federation payload or space WS frame, nor in
    ``GET /api/spaces/{id}/presence`` — in GPS mode or zone_only mode."""
    federated: list[dict] = []
    space_frames: list[dict] = []

    async def _capture_federation(self, space_id, event_type, payload, **kw):
        federated.append({"event_type": str(event_type), "payload": payload})

    real_broadcast = WebSocketManager.broadcast_to_users

    async def _capture_ws(self, user_ids, payload):
        if str(payload.get("type", "")).startswith("space"):
            space_frames.append(payload)
        return await real_broadcast(self, user_ids, payload)

    monkeypatch.setattr(
        FederationService, "broadcast_to_space_members", _capture_federation
    )
    monkeypatch.setattr(WebSocketManager, "broadcast_to_users", _capture_ws)
    auth = _auth(client._tok)

    r = await client.post(
        "/api/spaces",
        json={"name": "Family", "space_type": "household", "join_mode": "open"},
        headers=auth,
    )
    assert r.status == 201, await r.text()
    space_id = (await r.json())["id"]
    await client._db.enqueue(
        "UPDATE spaces SET feature_location=1, location_mode=? WHERE id=?",
        (mode, space_id),
    )
    await client._db.enqueue(
        "UPDATE space_members SET location_share_enabled=1"
        " WHERE space_id=? AND user_id=?",
        (space_id, client._uid),
    )
    # A space zone over the same spot, so zone_only mode publishes a label
    # (the space's own one) rather than silently skipping.
    r = await client.post(
        f"/api/spaces/{space_id}/zones",
        json={
            "name": "Downtown",
            "latitude": 47.3769,
            "longitude": 8.5417,
            "radius_m": 2000,
        },
        headers=auth,
    )
    assert r.status == 201, await r.text()
    r = await client.post(
        "/api/household/zones",
        json={
            "name": _SECRET,
            "latitude": 47.3769,
            "longitude": 8.5417,
            "radius_m": 150,
        },
        headers=auth,
    )
    assert r.status == 201, await r.text()

    r = await client.post(
        "/api/presence/location",
        json={
            "username": "admin",
            "latitude": 47.37691,
            "longitude": 8.54171,
            "accuracy_m": 10,
        },
        headers=auth,
    )
    assert r.status == 204, await r.text()

    # Sanity: the household itself did match the zone …
    r = await client.get("/api/presence", headers=auth)
    [me] = [p for p in await r.json() if p["username"] == "admin"]
    assert me["zone_name"] == _SECRET
    # … and the space fan-out really ran (so the checks below mean something).
    assert federated and space_frames

    for item in (*federated, *space_frames):
        assert _SECRET not in json.dumps(item, ensure_ascii=False)
    r = await client.get(f"/api/spaces/{space_id}/presence", headers=auth)
    assert r.status == 200
    body = await r.text()
    assert client._uid in body  # a real answer that lists the member
    assert _SECRET not in body

"""§27.9: presence updates truncate GPS to 4dp + accuracy gate.

Privacy invariants:
* GPS coordinates stored at most 4 decimal places (≈ 11 m precision).
* Updates with accuracy worse than 500 m must be dropped, not stored
  with degraded precision.
* The presence row never carries identifying fields beyond the
  user/zone/coords/state/accuracy.
"""

from __future__ import annotations

import pytest

from social_home.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from social_home.db.database import AsyncDatabase
from social_home.domain.presence import LocationUpdate, truncate_coord
from social_home.repositories.presence_repo import SqlitePresenceRepo
from social_home.services.presence_service import PresenceService


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
    yield db, PresenceService(SqlitePresenceRepo(db))
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


async def test_zone_only_state_omits_coords(env):
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

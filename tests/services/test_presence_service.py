"""Tests for social_home.services.presence_service."""

import pytest
from social_home.crypto import (
    generate_identity_keypair,
    derive_instance_id,
    derive_user_id,
)
from social_home.db.database import AsyncDatabase
from social_home.domain.presence import LocationUpdate
from social_home.repositories.presence_repo import SqlitePresenceRepo
from social_home.services.presence_service import PresenceService


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key, identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    uid = derive_user_id(kp.public_key, "anna")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", uid, "Anna"),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.svc = PresenceService(SqlitePresenceRepo(db))
    e.uid = uid
    yield e
    await db.shutdown()


async def test_update_and_list(env):
    """Update a location then list_presence returns it."""
    await env.svc.update_location(
        LocationUpdate(
            username="anna",
            state="home",
            zone_name="Home",
            latitude=52.3765,
            longitude=4.8957,
        )
    )
    result = await env.svc.list_presence()
    assert len(result) == 1
    assert result[0].username == "anna"
    assert result[0].state == "home"
    assert result[0].zone_name == "Home"


async def test_gps_truncation(env):
    """Coordinates are truncated to 4 decimal places."""
    await env.svc.update_location(
        LocationUpdate(
            username="anna",
            state="zone",
            zone_name="Work",
            latitude=52.37654321,
            longitude=4.89567890,
        )
    )
    p = await env.svc.get_presence("anna")
    assert p.latitude == 52.3765
    assert p.longitude == 4.8957


async def test_accuracy_gate(env):
    """GPS accuracy >500m nullifies coordinates but keeps zone."""
    await env.svc.update_location(
        LocationUpdate(
            username="anna",
            state="zone",
            zone_name="Park",
            latitude=52.37,
            longitude=4.89,
            gps_accuracy_m=600.0,
        )
    )
    p = await env.svc.get_presence("anna")
    assert p.latitude is None
    assert p.longitude is None
    assert p.zone_name == "Park"


async def test_accuracy_within_limit(env):
    """GPS accuracy <=500m keeps coordinates."""
    await env.svc.update_location(
        LocationUpdate(
            username="anna",
            state="zone",
            zone_name="Home",
            latitude=52.37,
            longitude=4.89,
            gps_accuracy_m=100.0,
        )
    )
    p = await env.svc.get_presence("anna")
    assert p.latitude is not None
    assert p.gps_accuracy_m == 100.0


async def test_get_presence_unknown(env):
    """get_presence for unknown user returns None."""
    assert await env.svc.get_presence("nobody") is None


async def test_invalid_state_rejected(env):
    """Invalid presence state raises ValueError."""
    with pytest.raises(ValueError, match="invalid"):
        await env.svc.update_location(
            LocationUpdate(
                username="anna",
                state="flying",
            )
        )


# ─── Remote PRESENCE_UPDATED ───────────────────────────────────────────────


async def test_apply_remote_persists_row(env):
    """A remote PRESENCE_UPDATED lands in remote_presence."""
    await env.svc.apply_remote(
        from_instance="peer-1",
        payload={
            "username": "bob",
            "state": "home",
            "zone_name": "Peer Home",
            "latitude": 52.37,
            "longitude": 4.89,
        },
    )
    row = await env.db.fetchone(
        "SELECT zone_name, state FROM remote_presence "
        "WHERE from_instance='peer-1' AND remote_username='bob'",
    )
    assert row is not None
    assert row["zone_name"] == "Peer Home"
    assert row["state"] == "home"


async def test_apply_remote_truncates_coords(env):
    await env.svc.apply_remote(
        from_instance="peer-2",
        payload={
            "username": "bob",
            "state": "away",
            "latitude": 52.37651111,
            "longitude": 4.89571111,
        },
    )
    row = await env.db.fetchone(
        "SELECT latitude, longitude FROM remote_presence "
        "WHERE from_instance='peer-2' AND remote_username='bob'",
    )
    assert row is not None
    assert abs(row["latitude"] - 52.3765) < 1e-6
    assert abs(row["longitude"] - 4.8957) < 1e-6


async def test_apply_remote_drops_low_accuracy(env):
    await env.svc.apply_remote(
        from_instance="peer-3",
        payload={
            "username": "bob",
            "state": "zone",
            "zone_name": "Far",
            "latitude": 52.37,
            "longitude": 4.89,
            "gps_accuracy_m": 600.0,
        },
    )
    row = await env.db.fetchone(
        "SELECT latitude, longitude, zone_name FROM remote_presence "
        "WHERE from_instance='peer-3' AND remote_username='bob'",
    )
    assert row["latitude"] is None
    assert row["longitude"] is None
    assert row["zone_name"] == "Far"


async def test_apply_remote_rejects_invalid_state(env):
    await env.svc.apply_remote(
        from_instance="peer-4",
        payload={"username": "bob", "state": "flying"},
    )
    row = await env.db.fetchone(
        "SELECT 1 FROM remote_presence "
        "WHERE from_instance='peer-4' AND remote_username='bob'",
    )
    assert row is None


async def test_apply_remote_ignores_empty_username(env):
    await env.svc.apply_remote(
        from_instance="peer-5",
        payload={"username": "", "state": "home"},
    )
    row = await env.db.fetchone(
        "SELECT 1 FROM remote_presence WHERE from_instance='peer-5'",
    )
    assert row is None

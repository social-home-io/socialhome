"""Tests for socialhome.services.presence_service."""

import pytest
from socialhome.crypto import (
    generate_identity_keypair,
    derive_instance_id,
    derive_user_id,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import PresenceUpdated
from socialhome.domain.presence import LocationUpdate
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.household_zone_repo import SqliteHouseholdZoneRepo
from socialhome.repositories.presence_repo import SqlitePresenceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.household_zone_service import HouseholdZoneService
from socialhome.services.presence_service import (
    PresenceService,
    UserNotFoundError,
)


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
    e.svc = PresenceService(SqlitePresenceRepo(db), SqliteUserRepo(db))
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


async def test_update_location_unknown_username_raises_user_not_found(env):
    """Pushing presence for a username that doesn't exist in ``users`` must
    raise :class:`UserNotFoundError` before the DB write — otherwise the
    FK on ``presence(username)`` surfaces as a generic IntegrityError that
    used to also poison every other write in the same writer batch."""
    with pytest.raises(UserNotFoundError) as excinfo:
        await env.svc.update_location(
            LocationUpdate(username="nobody", state="home"),
        )
    assert "nobody" in str(excinfo.value)

    # And the presence row was never persisted, so a sibling enqueue
    # in the same writer tick wouldn't have seen a FK violation.
    row = await env.db.fetchone("SELECT 1 FROM presence WHERE username='nobody'")
    assert row is None


# ─── Server-side GPS → household zone match ──────────────────────────────


@pytest.fixture
async def zoned(env):
    """``env`` with household zones Home (radius 100 m) and School, and a
    presence service that matches GPS against them."""
    zones = HouseholdZoneService(SqliteHouseholdZoneRepo(env.db))
    await zones.create_zone(
        actor_is_admin=True,
        name="Zuhause",
        latitude=52.5200,
        longitude=13.4050,
        radius_m=100,
        is_home=True,
    )
    await zones.create_zone(
        actor_is_admin=True,
        name="School",
        latitude=52.5300,
        longitude=13.4050,
        radius_m=200,
    )
    bus = EventBus()
    seen: list[PresenceUpdated] = []

    async def _on(event):
        seen.append(event)

    bus.subscribe(PresenceUpdated, _on)
    env.svc = PresenceService(
        SqlitePresenceRepo(env.db), SqliteUserRepo(env.db), bus, zone_matcher=zones
    )
    env.events = seen
    return env


async def _post_gps(env, lat, lon, *, accuracy=10.0, zone_name=None, state=None):
    await env.svc.update_location(
        LocationUpdate(
            username="anna",
            state=state or ("away" if not zone_name else "zone"),
            zone_name=zone_name,
            latitude=lat,
            longitude=lon,
            gps_accuracy_m=accuracy,
        )
    )
    return await env.svc.get_presence("anna")


async def test_gps_inside_home_zone_is_home(zoned):
    p = await _post_gps(zoned, 52.52005, 13.40502)
    assert (p.state, p.zone_name) == ("home", "home")
    assert (zoned.events[-1].state, zoned.events[-1].zone_name) == ("home", "home")


async def test_gps_inside_named_zone_is_zone(zoned):
    p = await _post_gps(zoned, 52.5301, 13.4050)
    assert (p.state, p.zone_name) == ("zone", "School")
    assert zoned.events[-1].zone_name == "School"


async def test_gps_outside_every_zone_is_away(zoned):
    p = await _post_gps(zoned, 48.1371, 11.5754)
    assert (p.state, p.zone_name) == ("away", None)


async def test_client_zone_name_is_kept(zoned):
    """The HA integration sends HA's zone state — never overridden."""
    p = await _post_gps(zoned, 52.5301, 13.4050, zone_name="Work", state="zone")
    assert (p.state, p.zone_name) == ("zone", "Work")


async def test_poor_accuracy_guesses_no_zone(zoned):
    p = await _post_gps(zoned, 52.5200, 13.4050, accuracy=900.0)
    assert (p.state, p.zone_name) == ("away", None)
    assert p.latitude is None


async def test_no_gps_guesses_no_zone(zoned):
    p = await _post_gps(zoned, None, None)
    assert (p.state, p.zone_name) == ("away", None)


async def test_match_uses_the_truncated_point(zoned):
    """The match runs on the stored 4-dp point, not raw device precision."""
    p = await _post_gps(zoned, 52.520049999, 13.405049999)
    assert p.latitude == 52.52 and p.zone_name == "home"

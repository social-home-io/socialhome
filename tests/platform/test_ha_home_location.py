"""Tests for the shared HA home location: the startup persistence helper
and the :class:`HaHomeLocationSource`."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import LocalHomeLocationUpdated
from socialhome.infrastructure.event_bus import EventBus
from socialhome.platform.adapter import HomeLocationSource
from socialhome.platform.ha_home_location import (
    HaHomeLocationSource,
    persist_home_location_from_ha,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    yield e
    await db.shutdown()


async def test_persist_writes_initial_value_and_publishes(env):
    """First boot — coords were NULL, HA reports a real value →
    written + LocalHomeLocationUpdated fired."""
    bus = EventBus()
    received: list[LocalHomeLocationUpdated] = []

    async def _record(e):
        received.append(e)

    bus.subscribe(LocalHomeLocationUpdated, _record)

    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=52.52,
        longitude=13.40,
    )

    row = await env.db.fetchone(
        "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'",
    )
    assert row["home_lat"] == 52.52
    assert row["home_lon"] == 13.40
    assert len(received) == 1
    assert received[0].latitude == 52.52
    assert received[0].longitude == 13.40


async def test_persist_truncates_to_4dp(env):
    """Inputs above 4dp precision are rounded — §25 invariant."""
    bus = EventBus()
    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=52.523456,
        longitude=13.401234,
    )
    row = await env.db.fetchone(
        "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'",
    )
    assert row["home_lat"] == 52.5235
    assert row["home_lon"] == 13.4012


async def test_persist_noop_when_unchanged(env):
    """Same value as already stored: no UPDATE, no event."""
    bus = EventBus()
    received: list[LocalHomeLocationUpdated] = []

    async def _record(e):
        received.append(e)

    bus.subscribe(LocalHomeLocationUpdated, _record)

    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=52.52,
        longitude=13.40,
    )
    received.clear()

    # Second call with same value.
    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=52.52,
        longitude=13.40,
    )
    assert received == []


async def test_persist_skips_zero_zero(env):
    """HA returns 0.0/0.0 when operator hasn't set a location.
    Don't write garbage; don't publish."""
    bus = EventBus()
    received: list[LocalHomeLocationUpdated] = []

    async def _record(e):
        received.append(e)

    bus.subscribe(LocalHomeLocationUpdated, _record)

    await persist_home_location_from_ha(
        db=env.db,
        bus=bus,
        latitude=0.0,
        longitude=0.0,
    )
    row = await env.db.fetchone(
        "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'",
    )
    assert row["home_lat"] is None
    assert row["home_lon"] is None
    assert received == []


# ── HaHomeLocationSource (the HA HomeLocationSource) ─────────────────────


class _Client:
    """``HaClient.get_config`` stand-in: the ``/api/config`` body, or
    ``None`` (the client's answer when HA didn't respond — it logs the
    WARNING itself)."""

    def __init__(self, config):
        self.config = config

    async def get_config(self):
        return self.config


class _Adapter:
    def __init__(self, client):
        self._c = client

    @property
    def _client(self):
        if self._c is None:
            raise RuntimeError("HaAdapter used before on_startup — no HaClient wired")
        return self._c


async def test_source_reads_latitude_longitude_from_api_config():
    """``GET /api/config`` answers ``latitude`` / ``longitude`` (HA REST API,
    developers.home-assistant.io/docs/api/rest) — rounded to 4 dp."""
    client = _Client(
        {"location_name": "Home", "latitude": 47.376912, "longitude": 8.541694}
    )
    source = HaHomeLocationSource(_Adapter(client))
    assert isinstance(source, HomeLocationSource)
    assert await source.get_home_location() == (47.3769, 8.5417)


async def test_source_zero_zero_means_ha_has_none():
    source = HaHomeLocationSource(_Adapter(_Client({"latitude": 0, "longitude": 0})))
    assert await source.get_home_location() is None


@pytest.mark.parametrize(
    "config",
    [
        None,  # HA didn't answer
        {},  # no location keys
        {"latitude": "47.3", "longitude": 8.5},
        {"latitude": 95.0, "longitude": 8.5},
        {"latitude": True, "longitude": 8.5},
    ],
)
async def test_source_unusable_answer_is_none(config):
    source = HaHomeLocationSource(_Adapter(_Client(config)))
    assert await source.get_home_location() is None


async def test_source_before_startup_is_none():
    """No client yet (``on_startup`` hasn't run) — nothing to report."""
    assert await HaHomeLocationSource(_Adapter(None)).get_home_location() is None

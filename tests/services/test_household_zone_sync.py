"""Tests for :class:`HouseholdZoneSync` — the platform → household zone
mirror (in-memory stubs; the route tests cover it on real SQLite)."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.domain.household_zone import (
    ExternalHouseholdZone,
    HouseholdZoneMirrorResult,
    HouseholdZoneSyncStatus,
    ZoneSourceUnavailableError,
)
from socialhome.services.household_zone_sync import (
    SYNC_ENABLED_KEY,
    HouseholdZoneSync,
)

HOME = ExternalHouseholdZone(
    external_id="zone.home",
    name="Home",
    latitude=47.3769,
    longitude=8.5417,
    radius_m=100,
    is_home=True,
    icon="🏠",
)
WORK = ExternalHouseholdZone(
    external_id="zone.work", name="Work", latitude=47.4, longitude=8.5, radius_m=150
)


class _Config:
    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value

    async def set_many(self, items):
        self.values.update(items)

    async def delete(self, key):
        self.values.pop(key, None)


class _ZoneService:
    """Records every ``mirror_external`` call."""

    def __init__(self) -> None:
        self.calls: list[list[ExternalHouseholdZone]] = []
        self.fail: Exception | None = None
        self.synced = 0

    async def mirror_external(self, source):
        if self.fail is not None:
            raise self.fail
        self.calls.append(list(source))
        removed = self.synced if not source else 0
        self.synced = len(source)
        return HouseholdZoneMirrorResult(
            upserted=len(source), removed=removed, skipped=0, changed=True
        )


class _Source:
    def __init__(self, zones=None) -> None:
        self.zones = zones if zones is not None else [HOME, WORK]
        self.fail = False
        self.reads = 0

    async def list_zones(self):
        self.reads += 1
        if self.fail:
            raise ZoneSourceUnavailableError("Home Assistant didn't answer")
        return list(self.zones)


@pytest.fixture
def zones():
    return _ZoneService()


@pytest.fixture
def source():
    return _Source()


@pytest.fixture
def config():
    return _Config()


@pytest.fixture
def sync(zones, config, source):
    return HouseholdZoneSync(zones, config, source, interval_seconds=0.01)


# ── option ───────────────────────────────────────────────────────────────


async def test_enabled_by_default(sync):
    assert await sync.is_enabled() is True
    assert sync.available


@pytest.mark.parametrize("raw, expected", [("0", False), ("false", False), ("1", True)])
async def test_enabled_reads_the_stored_option(zones, source, raw, expected):
    sync = HouseholdZoneSync(zones, _Config(**{SYNC_ENABLED_KEY: raw}), source)
    assert await sync.is_enabled() is expected


async def test_status_before_any_run(sync):
    assert await sync.status() == HouseholdZoneSyncStatus(
        available=True, enabled=True, last_synced_at=None, last_error=None
    )


async def test_without_a_source_nothing_runs(zones, config):
    sync = HouseholdZoneSync(zones, config, None)
    assert not sync.available
    await sync.sync_now()
    await sync.start()
    await sync.stop()
    assert zones.calls == []
    assert (await sync.status()).available is False
    with pytest.raises(ValueError):
        await sync.set_enabled(True)


async def test_set_enabled_refuses_a_non_bool(sync):
    with pytest.raises(ValueError):
        await sync.set_enabled("yes")


# ── a run ────────────────────────────────────────────────────────────────


async def test_sync_on_mirrors_the_source(sync, zones):
    await sync.sync_now()
    assert zones.calls == [[HOME, WORK]]
    status = await sync.status()
    assert status.last_synced_at is not None and status.last_error is None


async def test_failed_read_keeps_the_synced_zones(sync, zones, source, caplog):
    """A failed fetch is NOT "HA has no zones" — no mirror, no wipe."""
    await sync.sync_now()
    first = (await sync.status()).last_synced_at
    source.fail = True
    await sync.sync_now()
    assert zones.calls == [[HOME, WORK]]
    status = await sync.status()
    assert status.last_error == "Home Assistant didn't answer"
    assert status.last_synced_at == first
    assert "platform zones unavailable" in caplog.text
    assert any(r.levelname == "WARNING" for r in caplog.records)


async def test_a_good_run_clears_the_error(sync, source):
    source.fail = True
    await sync.sync_now()
    source.fail = False
    await sync.sync_now()
    assert (await sync.status()).last_error is None


async def test_empty_source_does_mirror_empty(sync, zones, source):
    """HA truly has no zones → the synced ones go."""
    source.zones = []
    await sync.sync_now()
    assert zones.calls == [[]]


async def test_mirror_failure_is_logged_and_kept(sync, zones, caplog):
    zones.fail = RuntimeError("boom /internal/path secret")
    await sync.sync_now()  # never raises
    # The admin API gets a fixed summary; the detail stays in the log.
    assert (await sync.status()).last_error == "sync failed"
    assert "sync failed" in caplog.text
    assert "/internal/path" in caplog.text


# ── turning it off / on ──────────────────────────────────────────────────


async def test_turning_off_removes_synced_zones_once_then_idles(
    sync, zones, source, config
):
    await sync.sync_now()
    status = await sync.set_enabled(False)
    assert config.values[SYNC_ENABLED_KEY] == "0"
    assert zones.calls[-1] == []
    assert status == HouseholdZoneSyncStatus(available=True, enabled=False)
    reads = source.reads
    calls = len(zones.calls)
    await sync.sync_now()
    await sync.sync_now()
    assert len(zones.calls) == calls  # idles
    assert source.reads == reads  # never reads HA while off


async def test_turning_on_pulls_right_away(zones, source):
    config = _Config(**{SYNC_ENABLED_KEY: "0"})
    sync = HouseholdZoneSync(zones, config, source)
    status = await sync.set_enabled(True)
    assert config.values[SYNC_ENABLED_KEY] == "1"
    assert zones.calls == [[HOME, WORK]]
    assert status.enabled and status.last_synced_at is not None


async def test_off_on_off_clears_again(sync, zones):
    await sync.set_enabled(False)
    await sync.set_enabled(True)
    await sync.set_enabled(False)
    assert zones.calls == [[], [HOME, WORK], []]


async def test_status_hides_a_stale_error_while_off(sync, source):
    source.fail = True
    await sync.sync_now()
    status = await sync.set_enabled(False)
    assert status.last_error is None


# ── serialised runs ──────────────────────────────────────────────────────


async def test_runs_never_overlap(sync, source):
    active = 0
    peak = 0
    real = source.list_zones

    async def _slow():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return await real()

    source.list_zones = _slow
    await asyncio.gather(sync.sync_now(), sync.sync_now(), sync.sync_now())
    assert peak == 1


# ── lifecycle ────────────────────────────────────────────────────────────


async def test_loop_runs_at_start_then_periodically(sync, zones):
    await sync.start()
    await sync.start()  # idempotent
    try:
        for _ in range(200):
            if len(zones.calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await sync.stop()
    assert len(zones.calls) >= 2
    n = len(zones.calls)
    await asyncio.sleep(0.05)
    assert len(zones.calls) == n  # stopped


async def test_stop_without_start_is_safe(sync):
    await sync.stop()


async def test_stop_cancels_a_stuck_run(zones, config):
    class _Stuck:
        async def list_zones(self):
            await asyncio.sleep(3600)

    sync = HouseholdZoneSync(zones, config, _Stuck())
    await sync.start()
    await asyncio.sleep(0.01)
    task = sync._task
    sync_stop = asyncio.create_task(sync.stop())
    # stop() waits up to 5 s, then cancels; shorten by cancelling now.
    task.cancel()
    await sync_stop
    assert sync._task is None


async def test_off_forgets_the_last_sync_time(sync, source):
    """Off → on with a failing read must not show the old sync time."""
    await sync.sync_now()
    await sync.set_enabled(False)
    source.fail = True
    status = await sync.set_enabled(True)
    assert status.last_synced_at is None
    assert status.last_error == "Home Assistant didn't answer"

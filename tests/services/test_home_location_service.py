"""The household's own home location — set here (standalone) or mirrored
read-only from the platform (Home Assistant)."""

from __future__ import annotations

import pytest

from socialhome.domain.events import LocalHomeLocationUpdated
from socialhome.domain.home_location import (
    HomeLocation,
    HomeLocationFromPlatformError,
    InvalidHomeLocationError,
)
from socialhome.services.home_location_service import HomeLocationService


class _Repo:
    """In-memory stand-in for the two federation-repo methods used."""

    def __init__(self, lat=None, lon=None, *, identity=True) -> None:
        self.lat, self.lon = lat, lon
        self.identity = identity
        self.writes: list[tuple] = []

    async def get_local_identity(self):
        if not self.identity:
            return None
        return {"instance_id": "iid", "home_lat": self.lat, "home_lon": self.lon}

    async def set_local_home_location(self, latitude, longitude):
        self.writes.append((latitude, longitude))
        self.lat, self.lon = latitude, longitude


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)


class _Source:
    def __init__(self, point) -> None:
        self.point = point
        self.calls = 0

    async def get_home_location(self):
        self.calls += 1
        return self.point


def _svc(repo, *, source=None, bus=None):
    return HomeLocationService(repo, bus=bus, source=source)


# ── Standalone: the household sets its home ──────────────────────────────


async def test_get_without_a_home():
    assert await _svc(_Repo()).get() == HomeLocation(None, None, from_platform=False)


async def test_get_without_an_identity_row_is_no_home():
    home = await _svc(_Repo(identity=False)).get()
    assert home == HomeLocation(None, None, from_platform=False)


async def test_set_stores_4dp_and_announces_it():
    repo, bus = _Repo(), _Bus()
    home = await _svc(repo, bus=bus).set(47.376912, 8.541694)
    assert home == HomeLocation(47.3769, 8.5417, from_platform=False)
    assert repo.writes == [(47.3769, 8.5417)]
    # Peers (share_home on) and the household's own tabs hear about it.
    assert [type(e) for e in bus.events] == [LocalHomeLocationUpdated]
    assert (bus.events[0].latitude, bus.events[0].longitude) == (47.3769, 8.5417)
    assert await _svc(repo).get() == HomeLocation(47.3769, 8.5417, from_platform=False)


async def test_set_same_point_writes_and_announces_nothing():
    repo, bus = _Repo(47.3769, 8.5417), _Bus()
    await _svc(repo, bus=bus).set(47.37691, 8.54169)
    assert repo.writes == []
    assert bus.events == []


async def test_set_rejects_an_unusable_point():
    repo, bus = _Repo(), _Bus()
    with pytest.raises(InvalidHomeLocationError):
        await _svc(repo, bus=bus).set(91.0, 8.5)
    assert repo.writes == [] and bus.events == []


async def test_clear_removes_it_and_tells_peers_it_is_gone():
    repo, bus = _Repo(47.3769, 8.5417), _Bus()
    home = await _svc(repo, bus=bus).clear()
    assert home == HomeLocation(None, None, from_platform=False)
    assert repo.writes == [(None, None)]
    assert (bus.events[0].latitude, bus.events[0].longitude) == (None, None)


async def test_clear_without_a_home_is_quiet():
    repo, bus = _Repo(), _Bus()
    await _svc(repo, bus=bus).clear()
    assert repo.writes == [] and bus.events == []


async def test_no_bus_is_fine():
    repo = _Repo()
    await _svc(repo).set(47.3769, 8.5417)
    assert repo.writes == [(47.3769, 8.5417)]


# ── Home Assistant: the platform owns it ─────────────────────────────────


async def test_platform_home_is_read_again_and_stored_when_it_moved():
    """Changed in Home Assistant since the last start: reading the setting
    picks it up — stored and announced like any change."""
    repo, bus, source = _Repo(52.52, 13.405), _Bus(), _Source((47.3769, 8.5417))
    home = await _svc(repo, bus=bus, source=source).get()
    assert home == HomeLocation(47.3769, 8.5417, from_platform=True)
    assert repo.writes == [(47.3769, 8.5417)]
    assert len(bus.events) == 1


async def test_platform_home_unchanged_writes_nothing():
    repo, bus = _Repo(47.3769, 8.5417), _Bus()
    home = await _svc(repo, bus=bus, source=_Source((47.3769, 8.5417))).get()
    assert home.from_platform is True
    assert repo.writes == [] and bus.events == []


async def test_platform_without_an_answer_keeps_the_stored_home():
    """HA unreachable, or no location set there: show what we have."""
    repo, bus = _Repo(52.52, 13.405), _Bus()
    home = await _svc(repo, bus=bus, source=_Source(None)).get()
    assert home == HomeLocation(52.52, 13.405, from_platform=True)
    assert repo.writes == [] and bus.events == []


async def test_platform_home_cannot_be_set_or_cleared_here():
    repo, bus, source = _Repo(52.52, 13.405), _Bus(), _Source((52.52, 13.405))
    svc = _svc(repo, bus=bus, source=source)
    with pytest.raises(HomeLocationFromPlatformError):
        await svc.set(47.3769, 8.5417)
    with pytest.raises(HomeLocationFromPlatformError):
        await svc.clear()
    assert repo.writes == [] and bus.events == []
    # Refused before anything was asked of the platform.
    assert source.calls == 0

"""Tests for :mod:`socialhome.global_server.client_spread` — the WARNING for
a GFS whose per-client limits collapsed onto one address (a CDN / proxy
whose ``X-Forwarded-For`` the GFS can't use)."""

from __future__ import annotations

import logging

import pytest
from aiohttp import web

from socialhome.global_server.client_spread import (
    SPREAD_MIN_REQUESTS,
    SPREAD_WARN_EVERY_S,
    SPREAD_WINDOW_S,
    ClientSpreadWatch,
    build_client_spread_watch,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.public import ClientIpResolver
from socialhome.global_server.server import create_gfs_app


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


LOGGER = "socialhome.global_server.client_spread"


def _feed(watch: ClientSpreadWatch, clock: _Clock, buckets: list[str]) -> None:
    step = SPREAD_WINDOW_S / (len(buckets) + 1)
    for bucket in buckets:
        clock.now += step
        watch.observe(bucket)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def test_warns_when_one_bucket_takes_most_traffic(caplog):
    clock = _Clock()
    watch = ClientSpreadWatch(clock=clock)
    collapsed = ["162.158.1.1"] * (SPREAD_MIN_REQUESTS * 9 // 10)
    others = [f"198.51.100.{i % 200}" for i in range(SPREAD_MIN_REQUESTS // 10)]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _feed(watch, clock, collapsed + others)
        clock.now += SPREAD_WINDOW_S
        watch.observe("198.51.100.1")  # closes the window
    msgs = _warnings(caplog)
    assert len(msgs) == 1
    assert "162.158.1.1" in msgs[0]
    assert "trusted_proxies" in msgs[0]


def test_quiet_on_normal_spread(caplog):
    clock = _Clock()
    watch = ClientSpreadWatch(clock=clock)
    spread = [f"198.51.100.{i % 250}" for i in range(SPREAD_MIN_REQUESTS * 3)]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _feed(watch, clock, spread)
        clock.now += SPREAD_WINDOW_S
        watch.observe("198.51.100.1")
    assert _warnings(caplog) == []


def test_quiet_below_the_volume_floor(caplog):
    """A quiet GFS with one busy household is not a collapsed setup."""
    clock = _Clock()
    watch = ClientSpreadWatch(clock=clock)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _feed(watch, clock, ["203.0.113.5"] * (SPREAD_MIN_REQUESTS - 1))
        clock.now += SPREAD_WINDOW_S
        watch.observe("198.51.100.1")
    assert _warnings(caplog) == []


def test_warns_at_most_once_an_hour(caplog):
    clock = _Clock()
    watch = ClientSpreadWatch(clock=clock)
    windows = SPREAD_WARN_EVERY_S // SPREAD_WINDOW_S
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(windows + 2):
            _feed(watch, clock, ["162.158.1.1"] * SPREAD_MIN_REQUESTS)
            clock.now += 1
    # One at the first close, one again once the hour has passed.
    assert len(_warnings(caplog)) == 2


def test_distinct_buckets_are_bounded():
    clock = _Clock()
    watch = ClientSpreadWatch(clock=clock, max_buckets=100)
    for i in range(10_000):
        watch.observe(f"10.{i // 256 % 256}.{i % 256}.1")
    assert watch.tracked_buckets <= 100


@pytest.mark.parametrize("path", ["/healthz", "/cluster/sync"])
async def test_middleware_skips_health_and_cluster(aiohttp_client, path):
    seen: list[str] = []

    class _Watch:
        def observe(self, bucket: str) -> None:
            seen.append(bucket)

    app = web.Application(
        middlewares=[build_client_spread_watch(ClientIpResolver(()), _Watch())]
    )

    async def ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_route("*", "/{tail:.*}", ok)
    client = await aiohttp_client(app)
    assert (await client.get(path)).status == 200
    assert seen == []
    assert (await client.get("/spaces/abc")).status == 200
    assert len(seen) == 1


async def test_middleware_counts_the_resolved_bucket(aiohttp_client):
    """It counts what the limiters count: the resolver's bucket, so a
    collapsed ``X-Forwarded-For`` shows up as one bucket."""
    seen: list[str] = []

    class _Watch:
        def observe(self, bucket: str) -> None:
            seen.append(bucket)

    resolver = ClientIpResolver(("127.0.0.0/8",))
    app = web.Application(middlewares=[build_client_spread_watch(resolver, _Watch())])

    async def ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", ok)
    client = await aiohttp_client(app)
    await client.get("/", headers={"X-Forwarded-For": "162.158.1.1"})
    assert seen == ["162.158.1.1"]


def test_gfs_app_runs_the_watch_before_the_limits(tmp_dir):
    """Wired ahead of every rate limit, so refused (429) requests count."""
    app = create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url="http://gfs.test",
            data_dir=str(tmp_dir),
            instance_id="gfs-test",
        )
    )
    names = [m.__name__ for m in app.middlewares]
    assert names.index("_spread_watch") < names.index("_rate_limit")

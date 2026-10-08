"""Tests for :class:`GfsRouteDiscoveryScheduler` (v_53)."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.gfs_route_discovery_scheduler import (
    TRIGGER_COALESCE_S,
    GfsRouteDiscoveryScheduler,
)
from socialhome.services.gfs_route_discovery_service import (
    ROUTE_DISCOVERY_INTERVAL_S,
    ROUTE_DISCOVERY_JITTER_S,
)


class _Service:
    def __init__(self, *, fail: bool = False) -> None:
        self.rounds = 0
        self.expiries = 0
        self.fail = fail
        self.ran = asyncio.Event()

    async def expire_stale_routes(self) -> int:
        self.expiries += 1
        if self.fail:
            raise RuntimeError("db down")
        return 0

    async def probe_all(self) -> int:
        self.rounds += 1
        self.ran.set()
        if self.fail:
            raise RuntimeError("relay down")
        return 1


def test_defaults_are_a_day_plus_minus_an_hour_and_a_30s_coalesce():
    assert ROUTE_DISCOVERY_INTERVAL_S == 24 * 3600
    assert ROUTE_DISCOVERY_JITTER_S == 3600
    assert TRIGGER_COALESCE_S == 30


def test_next_delay_stays_within_the_jitter_bounds():
    sched = GfsRouteDiscoveryScheduler(_Service())
    for _ in range(500):
        delay = sched.next_delay()
        assert (
            ROUTE_DISCOVERY_INTERVAL_S - ROUTE_DISCOVERY_JITTER_S
            <= delay
            <= ROUTE_DISCOVERY_INTERVAL_S + ROUTE_DISCOVERY_JITTER_S
        )


def test_next_delay_uses_the_injected_jitter_and_never_goes_negative():
    seen: list[tuple[float, float]] = []

    def _low(a: float, b: float) -> float:
        seen.append((a, b))
        return a

    sched = GfsRouteDiscoveryScheduler(
        _Service(), interval_seconds=10.0, jitter_seconds=20.0, uniform=_low
    )
    assert sched.next_delay() == 0.0
    assert seen == [(-20.0, 20.0)]


async def test_the_timer_runs_expiry_then_a_probe_round():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(svc, interval_seconds=0.01, jitter_seconds=0.0)
    await sched.start()
    await asyncio.wait_for(svc.ran.wait(), timeout=2)
    await sched.stop()
    assert svc.rounds >= 1
    assert svc.expiries >= svc.rounds


async def test_triggers_inside_the_window_coalesce_into_one_round():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(
        svc, interval_seconds=3600.0, jitter_seconds=0.0, coalesce_seconds=0.05
    )
    await sched.start()
    for _ in range(10):
        sched.trigger()
        await asyncio.sleep(0)
    await asyncio.wait_for(svc.ran.wait(), timeout=2)
    await asyncio.sleep(0.1)
    await sched.stop()
    assert svc.rounds == 1


async def test_stop_during_the_coalesce_window_runs_nothing():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(
        svc, interval_seconds=3600.0, jitter_seconds=0.0, coalesce_seconds=60.0
    )
    await sched.start()
    sched.trigger()
    await asyncio.sleep(0.01)
    await asyncio.wait_for(sched.stop(), timeout=2)
    assert svc.rounds == 0


async def test_stop_cuts_a_long_wait_short():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(svc)
    await sched.start()
    await asyncio.wait_for(sched.stop(), timeout=2)
    assert svc.rounds == 0
    assert sched._task is None


async def test_start_is_idempotent_and_restartable():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(svc)
    await sched.start()
    task = sched._task
    await sched.start()
    assert sched._task is task
    await sched.stop()
    await sched.start()
    assert sched._task is not None and not sched._task.done()
    await sched.stop()


async def test_stop_without_start_is_safe():
    await GfsRouteDiscoveryScheduler(_Service()).stop()


async def test_a_failing_round_does_not_kill_the_loop():
    svc = _Service(fail=True)
    sched = GfsRouteDiscoveryScheduler(svc, interval_seconds=0.01, jitter_seconds=0.0)
    await sched.run_once()
    await sched.start()
    await asyncio.sleep(0.1)
    await sched.stop()
    assert svc.rounds >= 2

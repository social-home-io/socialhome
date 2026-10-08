"""Tests for :class:`GfsRouteDiscoveryScheduler` (v_53).

No test waits on wall-clock sleeps: rounds are observed through events the
fake service sets, and the trigger-gap arithmetic runs on a fake clock.
"""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.gfs_route_discovery_scheduler import (
    MIN_TRIGGER_GAP_S,
    TRIGGER_COALESCE_S,
    GfsRouteDiscoveryScheduler,
)
from socialhome.services.gfs_route_discovery_service import (
    ROUTE_DISCOVERY_INTERVAL_S,
    ROUTE_DISCOVERY_JITTER_S,
)


class _Service:
    def __init__(self, *, fail: bool = False, rounds_wanted: int = 1) -> None:
        self.rounds = 0
        self.expiries = 0
        self.fail = fail
        self.rounds_wanted = rounds_wanted
        self.done = asyncio.Event()
        self.should_stop_seen: list[object] = []

    async def expire_stale_routes(self) -> int:
        self.expiries += 1
        if self.fail:
            raise RuntimeError("db down")
        return 0

    async def probe_all(self, *, should_stop=None) -> int:
        self.should_stop_seen.append(should_stop)
        self.rounds += 1
        if self.rounds >= self.rounds_wanted:
            self.done.set()
        if self.fail:
            raise RuntimeError("relay down")
        return 1


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def _yield(n: int = 5) -> None:
    """Let the loop task run up to its next real wait (no wall-clock time)."""
    for _ in range(n):
        await asyncio.sleep(0)


def test_defaults():
    assert ROUTE_DISCOVERY_INTERVAL_S == 24 * 3600
    assert ROUTE_DISCOVERY_JITTER_S == 3600
    assert TRIGGER_COALESCE_S == 30
    assert MIN_TRIGGER_GAP_S == 600


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


async def test_a_trigger_waits_out_the_minimum_gap_since_the_last_round():
    clock = _Clock()
    sched = GfsRouteDiscoveryScheduler(_Service(), clock=clock)
    # Nothing ran yet: just the coalesce window.
    assert sched.trigger_delay() == TRIGGER_COALESCE_S
    await sched.run_once()
    clock.t = 60.0
    assert sched.trigger_delay() == MIN_TRIGGER_GAP_S - 60.0
    clock.t = MIN_TRIGGER_GAP_S + 1
    assert sched.trigger_delay() == TRIGGER_COALESCE_S


async def test_a_reconnect_flap_after_a_round_does_not_probe_again():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(
        svc,
        interval_seconds=3600.0,
        jitter_seconds=0.0,
        coalesce_seconds=0.0,
        min_trigger_gap_seconds=3600.0,
    )
    await sched.start()
    sched.trigger()
    await asyncio.wait_for(svc.done.wait(), timeout=2)
    # The connection flaps: more triggers right after the round.
    for _ in range(5):
        sched.trigger()
        await _yield()
    await asyncio.wait_for(sched.stop(), timeout=2)
    assert svc.rounds == 1


async def test_the_timer_runs_expiry_then_a_probe_round_with_the_stop_check():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(svc, interval_seconds=0.0, jitter_seconds=0.0)
    await sched.start()
    await asyncio.wait_for(svc.done.wait(), timeout=2)
    await sched.stop()
    assert svc.rounds >= 1
    assert svc.expiries >= svc.rounds
    # ``probe_all`` is handed the stop check, so stop() lands between peers.
    check = svc.should_stop_seen[0]
    assert callable(check) and check() is True


async def test_triggers_inside_the_window_coalesce_into_one_round():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(
        svc, interval_seconds=3600.0, jitter_seconds=0.0, coalesce_seconds=0.0
    )
    await sched.start()
    for _ in range(10):
        sched.trigger()
    await asyncio.wait_for(svc.done.wait(), timeout=2)
    await sched.stop()
    assert svc.rounds == 1


async def test_stop_during_the_coalesce_window_runs_nothing():
    svc = _Service()
    sched = GfsRouteDiscoveryScheduler(
        svc, interval_seconds=3600.0, jitter_seconds=0.0, coalesce_seconds=60.0
    )
    await sched.start()
    sched.trigger()
    await _yield()
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
    svc = _Service(fail=True, rounds_wanted=3)
    sched = GfsRouteDiscoveryScheduler(svc, interval_seconds=0.0, jitter_seconds=0.0)
    await sched.run_once()
    await sched.start()
    await asyncio.wait_for(svc.done.wait(), timeout=2)
    await sched.stop()
    assert svc.rounds >= 3

"""PollDeadlineScheduler — sweeps every poll service, ``_stop`` lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from socialhome.infrastructure.poll_deadline_scheduler import PollDeadlineScheduler


class _Service:
    def __init__(self, closes: int) -> None:
        self.closes = closes
        self.calls: list[datetime] = []

    async def close_due_polls(self, now: datetime | None = None) -> int:
        self.calls.append(now)
        closes, self.closes = self.closes, 0
        return closes


async def test_tick_once_sweeps_every_service():
    household, space = _Service(1), _Service(2)
    sched = PollDeadlineScheduler((household, space))
    now = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
    assert await sched.tick_once(now) == 3
    assert household.calls == [now] and space.calls == [now]
    assert await sched.tick_once(now) == 0


async def test_tick_once_defaults_to_now():
    service = _Service(0)
    await PollDeadlineScheduler((service,)).tick_once()
    (at,) = service.calls
    assert at.tzinfo is not None


async def test_start_sweeps_and_stop_ends_the_loop():
    service = _Service(1)
    sched = PollDeadlineScheduler((service,), interval_seconds=0.01)
    await sched.start()
    await sched.start()  # idempotent
    for _ in range(100):
        if len(service.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    await sched.stop()
    assert len(service.calls) >= 2
    calls = len(service.calls)
    await asyncio.sleep(0.05)
    assert len(service.calls) == calls
    await sched.stop()  # idempotent

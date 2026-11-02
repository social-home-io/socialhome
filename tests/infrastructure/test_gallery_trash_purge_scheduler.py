"""Tests for :class:`GalleryTrashPurgeScheduler`."""

from __future__ import annotations

import asyncio

from socialhome.infrastructure.gallery_trash_purge_scheduler import (
    GalleryTrashPurgeScheduler,
)


class _Gallery:
    def __init__(self, released: int = 2) -> None:
        self.calls = 0
        self._released = released

    async def purge_expired_trash(self) -> int:
        self.calls += 1
        return self._released


class _Boom:
    def __init__(self) -> None:
        self.calls = 0

    async def purge_expired_trash(self) -> int:
        self.calls += 1
        raise RuntimeError("disk gone")


async def test_a_tick_purges_the_expired_trash():
    gallery = _Gallery()
    sched = GalleryTrashPurgeScheduler(gallery, interval_seconds=3600)
    await sched.start()
    await asyncio.sleep(0.05)
    await sched.stop()
    assert gallery.calls == 1


async def test_nothing_to_purge_is_a_quiet_tick():
    gallery = _Gallery(released=0)
    sched = GalleryTrashPurgeScheduler(gallery, interval_seconds=3600)
    await sched.start()
    await asyncio.sleep(0.05)
    await sched.stop()
    assert gallery.calls == 1


async def test_a_failing_pass_never_stops_the_loop():
    boom = _Boom()
    sched = GalleryTrashPurgeScheduler(boom, interval_seconds=0.01)
    await sched.start()
    await asyncio.sleep(0.1)
    await sched.stop()
    assert boom.calls >= 2


async def test_start_is_idempotent_and_stop_ends_the_task():
    gallery = _Gallery()
    sched = GalleryTrashPurgeScheduler(gallery, interval_seconds=3600)
    await sched.start()
    task = sched._task
    await sched.start()
    assert sched._task is task
    await sched.stop()
    assert sched._task is None
    assert task is not None and task.done()


async def test_stop_before_start_is_harmless():
    await GalleryTrashPurgeScheduler(_Gallery()).stop()

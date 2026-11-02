"""Tests for socialhome.services.gallery_upload_batcher — one upload batch,
one :class:`GalleryItemsAdded` (household gallery D9)."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.domain.events import GalleryItemsAdded, GalleryItemUploaded
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services import gallery_upload_batcher as mod
from socialhome.services.gallery_upload_batcher import GalleryUploadBatcher


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env():
    bus = EventBus()
    clock = _Clock()
    batcher = GalleryUploadBatcher(
        bus, quiet_seconds=90, max_seconds=600, tick_seconds=0.01, clock=clock
    )
    batcher.wire()
    seen: list[GalleryItemsAdded] = []

    async def collect(event: GalleryItemsAdded) -> None:
        seen.append(event)

    bus.subscribe(GalleryItemsAdded, collect)
    return bus, clock, batcher, seen


def _up(album: str, who: str, kind: str = "photo", space_id: str | None = None):
    return GalleryItemUploaded(
        item_id=f"i-{album}-{who}",
        album_id=album,
        item_type=kind,
        uploader=who,
        space_id=space_id,
    )


async def test_uploads_close_together_are_one_batch(env):
    bus, clock, batcher, seen = env
    for _ in range(11):
        await bus.publish(_up("a1", "anna"))
        clock.now += 20  # a slow phone: one photo every 20 s
    await bus.publish(_up("a1", "anna", "video"))
    assert await batcher.flush_due() == 0  # still uploading
    clock.now += 89
    assert await batcher.flush_due() == 0
    clock.now += 1
    assert await batcher.flush_due() == 1
    [event] = seen
    assert (event.album_id, event.uploader) == ("a1", "anna")
    assert (event.photos, event.videos, event.count) == (11, 1, 12)
    assert batcher.pending == 0


async def test_each_uploader_and_album_is_its_own_batch(env):
    bus, clock, batcher, seen = env
    await bus.publish(_up("a1", "anna"))
    await bus.publish(_up("a1", "ben"))
    await bus.publish(_up("a2", "anna", space_id="s1"))
    clock.now += 90
    assert await batcher.flush_due() == 3
    got = {(e.album_id, e.uploader, e.space_id, e.count) for e in seen}
    assert got == {
        ("a1", "anna", None, 1),
        ("a1", "ben", None, 1),
        ("a2", "anna", "s1", 1),
    }


async def test_a_long_upload_is_cut_so_people_hear_while_it_runs(env):
    bus, clock, batcher, seen = env
    for _ in range(40):
        await bus.publish(_up("a1", "anna"))
        clock.now += 30
    # 40 × 30 s = 1200 s of steady uploads: cut at the 600 s mark.
    assert await batcher.flush_due() == 1
    assert seen[0].count == 40
    await bus.publish(_up("a1", "anna"))
    clock.now += 90
    await batcher.flush_due()
    assert [e.count for e in seen] == [40, 1]


async def test_past_the_bound_the_oldest_batch_goes_early(env, monkeypatch):
    bus, clock, batcher, seen = env
    monkeypatch.setattr(mod, "GALLERY_BATCH_MAX_PENDING", 2)
    await bus.publish(_up("a1", "anna"))
    clock.now += 1
    await bus.publish(_up("a2", "anna"))
    clock.now += 1
    await bus.publish(_up("a3", "anna"))
    assert [e.album_id for e in seen] == ["a1"]
    assert batcher.pending == 2


async def test_stop_publishes_what_is_still_open(env):
    bus, _clock, batcher, seen = env
    await batcher.start()
    await batcher.start()  # idempotent
    await bus.publish(_up("a1", "anna"))
    await batcher.stop()
    assert [e.count for e in seen] == [1]
    assert batcher.pending == 0


async def test_the_tick_publishes_quiet_batches(env):
    bus, clock, batcher, seen = env
    await bus.publish(_up("a1", "anna"))
    clock.now += 90
    await batcher.start()
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    await batcher.stop()
    assert [e.count for e in seen] == [1]


async def test_a_failing_subscriber_never_breaks_the_batcher(env):
    bus, clock, batcher, _seen = env

    async def boom(event: GalleryItemsAdded) -> None:
        raise RuntimeError("down")

    bus.subscribe(GalleryItemsAdded, boom)
    await bus.publish(_up("a1", "anna"))
    clock.now += 90
    await batcher.flush_due()
    assert batcher.pending == 0

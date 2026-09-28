"""GalleryAlbumTombstones — recently deleted space albums, in memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from socialhome.domain.events import GalleryAlbumDeleted
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.gallery_tombstones import GalleryAlbumTombstones

_T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_a_recorded_album_reads_as_deleted_in_its_space_only():
    t = GalleryAlbumTombstones()
    t.record("sp-1", "alb-1", at=_T0)
    assert t.is_deleted("sp-1", "alb-1")
    assert not t.is_deleted("sp-2", "alb-1")
    assert not t.is_deleted("sp-1", "alb-2")


def test_deleted_since_lists_only_newer_deletes_of_that_space():
    t = GalleryAlbumTombstones()
    t.record("sp-1", "old", at=_T0)
    t.record("sp-1", "new", at=_T0 + timedelta(hours=2))
    t.record("sp-2", "other", at=_T0 + timedelta(hours=2))
    since = (_T0 + timedelta(hours=1)).isoformat()
    assert t.deleted_since("sp-1", since) == ["new"]
    assert t.deleted_since("sp-1", "not a timestamp") == ["old", "new"]


def test_naive_timestamps_are_read_as_utc():
    t = GalleryAlbumTombstones()
    t.record("sp-1", "alb", at=_T0)
    assert t.deleted_since("sp-1", "2026-09-01 11:00:00") == ["alb"]
    assert t.deleted_since("sp-1", "2026-09-01 13:00:00") == []


def test_the_set_is_bounded_oldest_first():
    t = GalleryAlbumTombstones(max_entries=2)
    for n in range(3):
        t.record("sp-1", f"alb-{n}", at=_T0)
    assert not t.is_deleted("sp-1", "alb-0")
    assert t.is_deleted("sp-1", "alb-1") and t.is_deleted("sp-1", "alb-2")


async def test_every_space_album_delete_on_the_bus_is_recorded():
    """Local deletes and the ones applied from another household alike —
    both publish ``GalleryAlbumDeleted``. Household albums never federate,
    so they are not recorded."""
    bus = EventBus()
    t = GalleryAlbumTombstones()
    t.wire(bus)
    await bus.publish(GalleryAlbumDeleted(album_id="alb-1", space_id="sp-1"))
    await bus.publish(GalleryAlbumDeleted(album_id="alb-h", space_id=None))
    assert t.is_deleted("sp-1", "alb-1")
    assert t.deleted_since("sp-1", "2000-01-01T00:00:00+00:00") == ["alb-1"]
    assert not t.is_deleted("sp-1", "alb-h")

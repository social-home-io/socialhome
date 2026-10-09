"""LegacyAlbumDeletes — overtaking album deletes no tombstone row proves."""

from __future__ import annotations

from socialhome.services.legacy_album_deletes import LegacyAlbumDeletes


def test_a_recorded_album_reads_as_deleted_in_its_space_only():
    t = LegacyAlbumDeletes()
    t.record("sp-1", "alb-1")
    assert t.is_deleted("sp-1", "alb-1")
    assert not t.is_deleted("sp-2", "alb-1")
    assert not t.is_deleted("sp-1", "alb-2")


def test_the_set_is_bounded_oldest_first():
    t = LegacyAlbumDeletes(max_entries=2)
    for n in range(3):
        t.record("sp-1", f"alb-{n}")
    assert not t.is_deleted("sp-1", "alb-0")
    assert t.is_deleted("sp-1", "alb-1") and t.is_deleted("sp-1", "alb-2")


def test_re_recording_refreshes_an_entry():
    t = LegacyAlbumDeletes(max_entries=2)
    t.record("sp-1", "a")
    t.record("sp-1", "b")
    t.record("sp-1", "a")  # a is newest now
    t.record("sp-1", "c")
    assert t.is_deleted("sp-1", "a") and not t.is_deleted("sp-1", "b")

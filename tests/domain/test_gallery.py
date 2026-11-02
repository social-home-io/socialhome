"""Tests for the gallery domain types (``socialhome/domain/gallery.py``)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.gallery import (
    ALBUM_VIEW_FIELDS,
    AUDIENCE_CHOSEN,
    AUDIENCE_EVERYONE,
    AUDIENCES,
    GALLERY_MAX_UPLOAD_BYTES,
    GALLERY_SEEN_MAX,
    GALLERY_VIDEO_MAX_QUEUED,
    GALLERY_VIDEO_MAX_SECONDS,
    GALLERY_VIDEO_MAX_UPLOAD_BYTES,
    TRASH_RETENTION_DAYS,
    GalleryAlbum,
    GalleryAlbumAudience,
    GalleryItem,
    GallerySeen,
    GallerySettings,
    GalleryUpload,
    StashedUpload,
    UploadLimits,
    UploadOutcome,
    is_video_upload,
    video_fits,
)


def _item() -> GalleryItem:
    return GalleryItem(
        id="it-1",
        album_id="al-1",
        uploaded_by="u-1",
        item_type="photo",
        url="api/media/a.webp",
        thumbnail_url="api/media/a-t.webp",
        width=2560,
        height=1920,
    )


def test_the_trash_keeps_thirty_days():
    assert TRASH_RETENTION_DAYS == 30


def test_audiences_match_the_schema_check():
    assert AUDIENCES == {AUDIENCE_EVERYONE, AUDIENCE_CHOSEN} == {"everyone", "chosen"}


def test_an_upload_is_created_unless_said_otherwise():
    up = GalleryUpload(_item())
    assert up.outcome is UploadOutcome.CREATED
    assert UploadOutcome.ALREADY_HERE.value == "already_here"
    assert UploadOutcome.RESTORED.value == "restored"


def test_an_album_audience_defaults_to_everyone():
    aud = GalleryAlbumAudience(album_id="al-1")
    assert aud.audience == AUDIENCE_EVERYONE and aud.user_ids == frozenset()
    with pytest.raises(dataclasses.FrozenInstanceError):
        aud.audience = AUDIENCE_CHOSEN  # type: ignore[misc]


def test_the_wire_shapes_carry_no_library_field():
    """The audience and the upload digest are household-only: the digest is
    no field of the item, and the album's ``audience`` / ``locked`` are
    :data:`ALBUM_VIEW_FIELDS` the §25.6 exporter drops — never on the wire."""
    item_fields = {f.name for f in dataclasses.fields(GalleryItem)}
    assert "content_sha256" not in item_fields
    assert {"audience", "locked"} <= ALBUM_VIEW_FIELDS
    album = GalleryAlbum(id="a", space_id="sp", owner_user_id="u", name="n")
    assert "audience" not in album.to_federation_dict()
    assert "locked" not in album.to_federation_dict()
    assert "url" in _item().to_federation_dict()


def test_the_viewers_of_an_album():
    assert GalleryAlbumAudience(album_id="a").viewers is None
    private = GalleryAlbumAudience(
        album_id="a",
        audience=AUDIENCE_CHOSEN,
        user_ids=frozenset({"tom"}),
        owner_user_id="mia",
        guardian_ids=frozenset({"anna"}),
    )
    assert private.viewers == frozenset({"tom", "mia", "anna"})


def test_children_may_upload_by_default():
    assert GallerySettings().children_may_upload is True


# ─── Household videos (D4) ───────────────────────────────────────────────


def test_five_minutes_is_what_a_phone_shows_as_5_00():
    assert GALLERY_VIDEO_MAX_SECONDS == 300
    assert video_fits(300.0, 300)
    assert video_fits(300.99, 300)
    assert not video_fits(301.0, 300)
    assert not video_fits(432.0, 300)


def test_upload_caps_fit_five_minutes_of_4k_phone_video():
    # 5 min of 4K30 H.264 (~1.75 GB) fits; the photo / space cap is unchanged.
    assert GALLERY_VIDEO_MAX_UPLOAD_BYTES == 2 * 1024**3
    assert GALLERY_VIDEO_MAX_UPLOAD_BYTES > 1_750_000_000
    assert GALLERY_MAX_UPLOAD_BYTES == 100 * 1024**2
    assert GALLERY_VIDEO_MAX_QUEUED > 0


def test_which_uploads_go_the_video_way():
    assert is_video_upload("video/mp4")
    assert is_video_upload("video/quicktime")
    assert is_video_upload("application/octet-stream")
    assert not is_video_upload("image/jpeg")
    assert not is_video_upload("")


def test_upload_limits_and_stash_are_frozen():
    limits = UploadLimits(photo_bytes=1, video_bytes=2, video_seconds=3)
    stash = StashedUpload(path="/s/a.bin", size=4, sha256="ab" * 32)
    for obj, attr in ((limits, "video_bytes"), (stash, "size")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, 0)


# ─── "Neu" dot (D9) ──────────────────────────────────────────────────────

_T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def test_nothing_is_new_before_the_first_stamp():
    assert not GallerySeen().has_new("a1", "2026-11-02T11:00:00+00:00")


def test_photos_after_the_last_visit_are_new():
    seen = GallerySeen().stamped(_T0)
    assert seen.since == _T0.isoformat()
    # Never opened: newer than the stamp is new, older is not.
    assert seen.has_new("a1", (_T0 + timedelta(minutes=1)).isoformat())
    assert not seen.has_new("a1", (_T0 - timedelta(minutes=1)).isoformat())
    # SQLite's naive UTC reads the same.
    assert seen.has_new("a1", "2026-11-02 10:01:00")
    assert not seen.has_new("a1", None)
    assert not seen.has_new("a1", "garbage")
    opened = seen.opened("a1", _T0 + timedelta(hours=1))
    assert not opened.has_new("a1", (_T0 + timedelta(minutes=30)).isoformat())
    assert opened.has_new("a1", (_T0 + timedelta(hours=2)).isoformat())
    # Stamping again keeps the first stamp.
    assert opened.stamped(_T0 + timedelta(days=1)).since == _T0.isoformat()


def test_round_trips_through_its_dict():
    seen = GallerySeen().stamped(_T0).opened("a1", _T0)
    assert GallerySeen.from_dict(seen.to_dict()) == seen
    assert GallerySeen.from_dict("nope") == GallerySeen()


def test_the_bound_lets_the_oldest_visits_go_and_moves_since_up():
    seen = GallerySeen().stamped(_T0)
    for i in range(GALLERY_SEEN_MAX + 2):
        seen = seen.opened(f"a{i}", _T0 + timedelta(minutes=i))
    assert len(seen.albums) == GALLERY_SEEN_MAX
    assert "a0" not in seen.albums and "a1" not in seen.albums
    # A dropped album falls back to the newest dropped visit — never older.
    assert seen.since == (_T0 + timedelta(minutes=1)).isoformat()
    assert not seen.has_new("a0", (_T0 + timedelta(seconds=30)).isoformat())
    assert seen.has_new("a0", (_T0 + timedelta(minutes=2)).isoformat())


def test_has_new_is_a_view_field_never_exported():
    assert "has_new" in ALBUM_VIEW_FIELDS
    album = GalleryAlbum(id="a", space_id=None, owner_user_id="u", name="n")
    assert album.has_new is False

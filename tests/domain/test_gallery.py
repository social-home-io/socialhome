"""Tests for the gallery domain types (``socialhome/domain/gallery.py``)."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.gallery import (
    AUDIENCE_CHOSEN,
    AUDIENCE_EVERYONE,
    AUDIENCES,
    GALLERY_MAX_UPLOAD_BYTES,
    GALLERY_VIDEO_MAX_QUEUED,
    GALLERY_VIDEO_MAX_SECONDS,
    GALLERY_VIDEO_MAX_UPLOAD_BYTES,
    TRASH_RETENTION_DAYS,
    GalleryAlbum,
    GalleryAlbumAudience,
    GalleryItem,
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
    """The audience and the upload digest are household-only: neither is a
    field of the dataclasses the §25.6 sync exporter ships with ``asdict``."""
    album_fields = {f.name for f in dataclasses.fields(GalleryAlbum)}
    item_fields = {f.name for f in dataclasses.fields(GalleryItem)}
    assert "audience" not in album_fields
    assert "content_sha256" not in item_fields
    assert "url" in _item().to_federation_dict()


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

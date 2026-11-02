"""Tests for the gallery domain types (``socialhome/domain/gallery.py``)."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.gallery import (
    AUDIENCE_CHOSEN,
    AUDIENCE_EVERYONE,
    AUDIENCES,
    TRASH_RETENTION_DAYS,
    GalleryAlbum,
    GalleryAlbumAudience,
    GalleryItem,
    GalleryUpload,
    UploadOutcome,
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

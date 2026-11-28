"""The per-source sync policy of a GFS space's files (owner 2026-11-28)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.space_media_sync import (
    LIVE_WINDOW,
    SYNC_MAX_FILE_BYTES,
    SyncHint,
    SyncSource,
    parse_moment,
    syncs,
)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
SMALL = 400_000


def _syncs(hint: SyncHint, *, size: int = SMALL, name: str = "a.webp") -> bool:
    return syncs(hint, size=size, name=name, now=NOW)


@pytest.mark.parametrize(
    "source", [SyncSource.LIVE, SyncSource.GALLERY, SyncSource.PAGE]
)
def test_live_items_galleries_and_pages_always_sync(source):
    old = NOW - timedelta(days=400)
    assert _syncs(SyncHint(source, created_at=old))
    assert _syncs(SyncHint(source), name="v.webm", size=SYNC_MAX_FILE_BYTES)


def test_a_file_over_the_cap_is_on_view_only():
    for source in SyncSource:
        assert not _syncs(SyncHint(source), size=SYNC_MAX_FILE_BYTES + 1)


@pytest.mark.parametrize("source", [SyncSource.POST, SyncSource.COMMENT])
def test_posts_and_comments_sync_while_new(source):
    assert _syncs(SyncHint(source, created_at=NOW - LIVE_WINDOW + timedelta(hours=1)))
    assert not _syncs(
        SyncHint(source, created_at=NOW - LIVE_WINDOW - timedelta(hours=1))
    )
    # Age unknown: an old catch-up record, on view.
    assert not _syncs(SyncHint(source))
    # Fable M11: a stamp from the future is not "new" (skew allowed).
    assert _syncs(SyncHint(source, created_at=NOW + timedelta(minutes=4)))
    assert not _syncs(SyncHint(source, created_at=NOW + timedelta(days=30)))


@pytest.mark.parametrize("source", [SyncSource.CALENDAR, SyncSource.BAZAAR])
def test_covers_and_listings_sync_while_upcoming_or_active(source):
    assert _syncs(SyncHint(source, until=NOW + timedelta(days=1)))
    assert not _syncs(SyncHint(source, until=NOW - timedelta(minutes=1)))
    # No end (a recurring series): it goes on.
    assert _syncs(SyncHint(source))


def test_held_submissions_sync_their_pictures_only():
    hint = SyncHint(SyncSource.MODERATION)
    assert _syncs(hint, name="a.webp")
    assert not _syncs(hint, name="v.webm")


def test_parse_moment_reads_both_stored_shapes():
    assert parse_moment("2026-11-28 12:00:00") == NOW
    assert parse_moment("2026-11-28T12:00:00+00:00") == NOW
    assert parse_moment(NOW) == NOW
    assert parse_moment(datetime(2026, 11, 28, 12, 0)) == NOW
    assert parse_moment("nonsense") is None
    assert parse_moment(None) is None

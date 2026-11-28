"""Tests for StorageQuotaService.

Storage "used" is the size of the media directory on disk — every uploaded
blob (images, gallery photos, DM media, transcodes, …) lands there, so the
service measures the directory rather than summing post metadata (which only
ever covered FILE-type post attachments and reported 0 for a normal household).
"""

from __future__ import annotations

import pytest

from socialhome.services.storage_quota_service import (
    StorageQuotaExceeded,
    StorageQuotaService,
)


def _svc(media_dir, *, quota_bytes: int) -> StorageQuotaService:
    return StorageQuotaService(media_path=media_dir, quota_bytes=quota_bytes)


def _write(media_dir, rel: str, size: int) -> None:
    """Write a file of ``size`` bytes at ``media_dir/rel`` (creating dirs)."""
    p = media_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


# ─── current_usage_bytes ─────────────────────────────────────────────────


async def test_zero_usage_when_empty(media_dir):
    svc = _svc(media_dir, quota_bytes=1024)
    assert await svc.current_usage_bytes() == 0


async def test_zero_usage_when_dir_missing(tmp_path):
    # No uploads yet — the media dir may not exist. Must be 0, not a crash.
    svc = _svc(tmp_path / "does-not-exist", quota_bytes=1024)
    assert await svc.current_usage_bytes() == 0


async def test_sums_all_files_including_subdirs(media_dir):
    # The real-world reason the old impl reported 0: media lives as files in
    # the dir (and subdirs like transcode_src), not in post file_meta JSON.
    _write(media_dir, "photo.jpg", 100)
    _write(media_dir, "doc.pdf", 250)
    _write(media_dir, "transcode_src/clip.mp4", 650)
    svc = _svc(media_dir, quota_bytes=10_000)
    assert await svc.current_usage_bytes() == 1000


# ─── usage ───────────────────────────────────────────────────────────────


async def test_usage_returns_struct(media_dir):
    _write(media_dir, "a.bin", 200)
    svc = _svc(media_dir, quota_bytes=1000)
    u = await svc.usage()
    assert u.used_bytes == 200
    assert u.quota_bytes == 1000
    assert u.available_bytes == 800
    assert u.percent_used == 20.0


async def test_usage_percent_zero_when_quota_zero(media_dir):
    svc = _svc(media_dir, quota_bytes=0)
    u = await svc.usage()
    assert u.percent_used == 0.0


# ─── check_can_store ─────────────────────────────────────────────────────


async def test_check_can_store_passes_when_under_quota(media_dir):
    _write(media_dir, "a.bin", 100)
    svc = _svc(media_dir, quota_bytes=1000)
    await svc.check_can_store(500)  # 100 + 500 = 600 < 1000


async def test_check_can_store_raises_when_over_quota(media_dir):
    _write(media_dir, "a.bin", 800)
    svc = _svc(media_dir, quota_bytes=1000)
    with pytest.raises(StorageQuotaExceeded) as exc:
        await svc.check_can_store(500)
    assert exc.value.requested == 500
    assert exc.value.available == 200


async def test_check_can_store_disabled_when_quota_zero(media_dir):
    svc = _svc(media_dir, quota_bytes=0)
    await svc.check_can_store(10_000_000_000)  # must NOT raise


async def test_check_can_store_ignores_zero_or_negative(media_dir):
    svc = _svc(media_dir, quota_bytes=10)
    await svc.check_can_store(0)
    await svc.check_can_store(-5)


# ─── set_quota_bytes ───────────────────────────────────────────────────────


async def test_set_quota_bytes_updates_cap(media_dir):
    svc = _svc(media_dir, quota_bytes=1000)
    svc.set_quota_bytes(500)
    assert svc.quota_bytes == 500
    svc.set_quota_bytes(-1)  # <= 0 disables
    assert svc.quota_bytes == 0


def test_the_synced_space_files_storage_defaults_and_is_set(tmp_path):
    """Owner 2026-11-28: one GFS space's synced files default to 2 GiB on a
    household; an admin sets it at runtime, 0 (or less) = only what is
    opened."""
    svc = StorageQuotaService(media_path=tmp_path, quota_bytes=0)
    assert svc.space_sync_bytes == 2 * 1024 * 1024 * 1024
    svc.set_space_sync_bytes(5)
    assert svc.space_sync_bytes == 5
    svc.set_space_sync_bytes(-3)
    assert svc.space_sync_bytes == 0

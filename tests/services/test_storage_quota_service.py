"""Tests for StorageQuotaService.

Storage "used" is the size of the media directory on disk — every uploaded
blob (images, gallery photos, DM media, transcodes, …) lands there, so the
service measures the directory rather than summing post metadata (which only
ever covered FILE-type post attachments and reported 0 for a normal household).
"""

from __future__ import annotations

import pytest

from socialhome.domain.space_media_sync import DEFAULT_SPACE_SYNC_TOTAL_BYTES
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


# ─── Owner 2026-11-28: persisted settings, synced files yield to uploads ──


class _Config:
    """An in-memory ``instance_config`` (AbstractInstanceConfigRepo)."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, key):
        return self.rows.get(key)

    async def set(self, key, value):
        self.rows[key] = value

    async def set_many(self, items):
        self.rows.update(items)

    async def delete(self, key):
        self.rows.pop(key, None)


async def test_the_storage_settings_survive_a_restart(media_dir):
    """Owner 2026-11-28 (I5): the household quota, the synced-files storage
    per space and their total cap are kept in the instance config — a new
    process (a restart) starts from what the admin chose, not the config
    file's defaults."""
    config = _Config()
    svc = StorageQuotaService(
        media_path=media_dir, quota_bytes=1000, settings_repo=config
    )
    await svc.load()
    assert svc.space_sync_total_bytes == DEFAULT_SPACE_SYNC_TOTAL_BYTES
    await svc.update(quota_bytes=700, space_sync_bytes=30, space_sync_total_bytes=90)
    again = StorageQuotaService(
        media_path=media_dir, quota_bytes=1000, settings_repo=config
    )
    await again.load()
    assert (again.quota_bytes, again.space_sync_bytes) == (700, 30)
    assert again.space_sync_total_bytes == 90
    # One setting changed, the others stay as saved.
    await again.update(space_sync_bytes=0)
    third = StorageQuotaService(
        media_path=media_dir, quota_bytes=1000, settings_repo=config
    )
    await third.load()
    assert (third.quota_bytes, third.space_sync_bytes) == (700, 0)
    assert third.space_sync_total_bytes == 90


async def test_a_damaged_saved_setting_keeps_the_default(media_dir, caplog):
    config = _Config()
    config.rows["storage.settings"] = '{"quota_bytes": "lots", "space_sync_bytes": -4}'
    svc = StorageQuotaService(
        media_path=media_dir, quota_bytes=1000, settings_repo=config
    )
    await svc.load()
    assert svc.quota_bytes == 1000
    assert svc.space_sync_bytes == 2 * 1024 * 1024 * 1024
    config.rows["storage.settings"] = "not json"
    await svc.load()
    assert svc.quota_bytes == 1000


async def test_a_settings_change_tells_the_synced_files(media_dir):
    """A lower cap is applied at once (Fable I3): the synced files are told
    to sweep."""
    svc = StorageQuotaService(media_path=media_dir, quota_bytes=1000)
    told: list[str] = []

    async def evict(need):
        return 0

    svc.attach_synced_files(evict=evict, changed=lambda: told.append("x"))
    await svc.update(space_sync_total_bytes=5)
    assert told == ["x"]


async def test_an_upload_evicts_synced_files_before_it_is_refused(media_dir):
    """Owner 2026-11-28 (I4): synced space files live in the quota's FREE
    space — the household's own upload always wins: synced files are
    evicted first, and only what still does not fit is refused."""
    _write(media_dir, "own.webp", 600)
    _write(media_dir, "sg-synced.webp", 300)
    svc = StorageQuotaService(media_path=media_dir, quota_bytes=1000)
    asked: list[int] = []

    async def evict(need):
        asked.append(need)
        (media_dir / "sg-synced.webp").unlink()
        return 300

    svc.attach_synced_files(evict=evict, changed=lambda: None)
    await svc.check_can_store(250)  # 900 + 250 > 1000: 150 over
    assert asked == [150]
    with pytest.raises(StorageQuotaExceeded):
        await svc.check_can_store(500)  # nothing left to evict


async def test_free_for_sync_is_the_quotas_free_space(media_dir):
    _write(media_dir, "own.webp", 600)
    svc = StorageQuotaService(media_path=media_dir, quota_bytes=1000)
    assert await svc.free_for_sync() == 400
    assert (
        await StorageQuotaService(media_path=media_dir, quota_bytes=0).free_for_sync()
        is None
    )

"""Storage quota tracking + enforcement (§5.2 ``max_storage_bytes``).

A household has a single global byte budget. "Storage used" is the total
size of the **media directory on disk** — every uploaded blob lands there
(images, gallery photos, DM media, video/audio transcodes, profile
pictures…), so measuring the directory counts all real storage regardless
of which table references it. (The previous implementation summed only the
``file_meta_json`` of FILE-type posts, so a household whose storage is
photos / DMs / gallery — the normal case — always reported 0 bytes.)

The service:

* exposes :meth:`current_usage_bytes` (the media-dir size) for the
  GET /api/storage/usage endpoint;
* exposes :meth:`check_can_store` which raises
  :class:`StorageQuotaExceeded` when an upload would push the
  household over the configured cap.

The check is best-effort — it's a guard rail, not a security boundary.
A user racing two simultaneous uploads can technically exceed the cap
by a single file's size; that's acceptable for the v1 quota model.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..domain.space_media_sync import DEFAULT_SPACE_SYNC_BYTES

log = logging.getLogger(__name__)


# ─── Errors ──────────────────────────────────────────────────────────────


class StorageQuotaExceeded(Exception):
    """Upload would exceed the household's byte budget."""

    def __init__(self, requested: int, available: int):
        super().__init__(
            f"upload would exceed quota: needs {requested} bytes, "
            f"only {available} bytes available"
        )
        self.requested = requested
        self.available = available


@dataclass(slots=True, frozen=True)
class StorageUsage:
    used_bytes: int
    quota_bytes: int
    available_bytes: int

    @property
    def percent_used(self) -> float:
        if self.quota_bytes <= 0:
            return 0.0
        return (self.used_bytes / self.quota_bytes) * 100


# ─── Service ─────────────────────────────────────────────────────────────


class StorageQuotaService:
    """Per-household storage usage + quota enforcement."""

    __slots__ = ("_media_path", "_quota_bytes", "_space_sync_bytes")

    def __init__(
        self,
        *,
        media_path: str | Path,
        quota_bytes: int,
        space_sync_bytes: int = DEFAULT_SPACE_SYNC_BYTES,
    ) -> None:
        self._media_path = Path(media_path)
        self._quota_bytes = quota_bytes
        self._space_sync_bytes = max(int(space_sync_bytes), 0)

    @property
    def space_sync_bytes(self) -> int:
        """Storage for one GFS space's synced files (owner 2026-11-28): the
        files a household keeps without anyone opening them. 0 = only what
        it opens."""
        return self._space_sync_bytes

    def set_space_sync_bytes(self, value: int) -> None:
        """Mutate the synced-files storage at runtime, like
        :meth:`set_quota_bytes` (process-local; ``value <= 0`` = only what
        the household opens)."""
        self._space_sync_bytes = int(value) if value > 0 else 0

    @property
    def quota_bytes(self) -> int:
        return self._quota_bytes

    def set_quota_bytes(self, value: int) -> None:
        """Mutate the cap at runtime. ``value <= 0`` disables enforcement.

        Admin-only callers (see :class:`StorageQuotaView`). The change
        is process-local — operators who want persistence should reload
        the app with the new :class:`Config` value.
        """
        self._quota_bytes = int(value) if value > 0 else 0

    # ─── Usage ────────────────────────────────────────────────────────────

    async def current_usage_bytes(self) -> int:
        """Total bytes of every file under the media directory — the real
        on-disk household storage. Walked off the event loop (IO-bound)."""
        return await asyncio.to_thread(self._dir_size, self._media_path)

    @staticmethod
    def _dir_size(root: Path) -> int:
        """Recursively sum regular-file sizes under ``root`` (sync helper —
        runs in a worker thread). A missing dir (no uploads yet) is 0; an
        unreadable entry is skipped rather than failing the whole tally."""
        total = 0
        try:
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    try:
                        total += os.stat(os.path.join(dirpath, name)).st_size
                    except OSError:
                        continue
        except OSError:
            return total
        return total

    async def usage(self) -> StorageUsage:
        used = await self.current_usage_bytes()
        return StorageUsage(
            used_bytes=used,
            quota_bytes=self._quota_bytes,
            available_bytes=max(0, self._quota_bytes - used),
        )

    # ─── Enforcement ──────────────────────────────────────────────────────

    async def check_can_store(self, additional_bytes: int) -> None:
        """Raise :class:`StorageQuotaExceeded` if writing would overflow.

        ``additional_bytes`` is the size the caller wants to add. When
        the quota is ``<= 0`` the check is disabled — useful for tests
        and for operators that don't want a cap.
        """
        if self._quota_bytes <= 0:
            return
        if additional_bytes <= 0:
            return
        used = await self.current_usage_bytes()
        if used + additional_bytes > self._quota_bytes:
            available = max(0, self._quota_bytes - used)
            raise StorageQuotaExceeded(additional_bytes, available)

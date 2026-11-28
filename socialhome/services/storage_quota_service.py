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
  household over the configured cap — after evicting synced space files
  first (owner 2026-11-28: they only use the quota's free space, and the
  household's own upload always wins; :meth:`attach_synced_files`);
* holds the storage settings — the quota, the storage for one GFS space's
  synced files and their total cap across spaces — and keeps an admin's
  change in the instance config (``storage.settings``), so it survives a
  restart (:meth:`load`, :meth:`update`).

The check is best-effort — it's a guard rail, not a security boundary.
A user racing two simultaneous uploads can technically exceed the cap
by a single file's size; that's acceptable for the v1 quota model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import orjson

from ..domain.space_media_sync import (
    DEFAULT_SPACE_SYNC_BYTES,
    DEFAULT_SPACE_SYNC_TOTAL_BYTES,
)

if TYPE_CHECKING:
    from ..repositories.instance_config_repo import AbstractInstanceConfigRepo

log = logging.getLogger(__name__)

#: The ``instance_config`` key the admin's storage settings live under.
STORAGE_SETTINGS_KEY = "storage.settings"

#: The settings it holds (each an int >= 0; 0 = no quota / sync only what
#: is opened / no synced files at all).
SETTINGS_FIELDS: tuple[str, ...] = (
    "quota_bytes",
    "space_sync_bytes",
    "space_sync_total_bytes",
)

#: The largest value a storage setting takes (1 PiB; the route refuses
#: more, Fable B9).
MAX_SETTING_BYTES: int = 1 << 50

#: How old the media directory's size may be when a sync asks for the
#: quota's free space (a walk of the whole directory; uploads always
#: measure afresh).
FREE_FOR_SYNC_MAX_AGE_S: float = 30.0


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
    """Per-household storage usage + quota enforcement, and the storage
    settings (persisted in the instance config when *settings_repo* is
    given)."""

    __slots__ = (
        "_changed",
        "_evict",
        "_free_cache",
        "_media_path",
        "_quota_bytes",
        "_settings",
        "_space_sync_bytes",
        "_space_sync_total_bytes",
    )

    def __init__(
        self,
        *,
        media_path: str | Path,
        quota_bytes: int,
        space_sync_bytes: int = DEFAULT_SPACE_SYNC_BYTES,
        space_sync_total_bytes: int = DEFAULT_SPACE_SYNC_TOTAL_BYTES,
        settings_repo: AbstractInstanceConfigRepo | None = None,
    ) -> None:
        self._media_path = Path(media_path)
        self._quota_bytes = max(int(quota_bytes), 0)
        self._space_sync_bytes = max(int(space_sync_bytes), 0)
        self._space_sync_total_bytes = max(int(space_sync_total_bytes), 0)
        self._settings = settings_repo
        self._evict: Callable[[int], Awaitable[int]] | None = None
        self._changed: Callable[[], None] | None = None
        #: (monotonic time, media dir bytes) of the last walk for a sync.
        self._free_cache: tuple[float, int] | None = None

    def attach_synced_files(
        self,
        *,
        evict: Callable[[int], Awaitable[int]],
        changed: Callable[[], None],
    ) -> None:
        """The synced space files (owner 2026-11-28): *evict* frees at
        least the bytes asked for if it can (returns the bytes freed) — an
        upload that would not fit evicts them first; *changed* is told
        when a setting changed (a lower cap applies at once)."""
        self._evict = evict
        self._changed = changed

    async def load(self) -> None:
        """Take the admin's saved settings from the instance config (a value
        that does not read as an int >= 0 keeps the configured default)."""
        if self._settings is None:
            return
        try:
            raw = await self._settings.get(STORAGE_SETTINGS_KEY)
            saved = orjson.loads(raw) if raw else {}
        except Exception:
            log.warning("storage settings not read — the defaults apply", exc_info=True)
            return
        if not isinstance(saved, dict):
            return
        for field in SETTINGS_FIELDS:
            value = saved.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(self, "_" + field, value)

    async def update(
        self,
        *,
        quota_bytes: int | None = None,
        space_sync_bytes: int | None = None,
        space_sync_total_bytes: int | None = None,
    ) -> None:
        """Change the given settings (``<= 0`` = 0) and keep all three in
        the instance config; the synced files are told (a lower cap sweeps
        at once)."""
        values = {f: getattr(self, "_" + f) for f in SETTINGS_FIELDS}
        for field, value in (
            ("quota_bytes", quota_bytes),
            ("space_sync_bytes", space_sync_bytes),
            ("space_sync_total_bytes", space_sync_total_bytes),
        ):
            if value is not None:
                values[field] = min(max(int(value), 0), MAX_SETTING_BYTES)
        if self._settings is not None:
            # Kept first (Fable B9): a failed write changes nothing.
            await self._settings.set(
                STORAGE_SETTINGS_KEY, orjson.dumps(values).decode()
            )
        for field, value in values.items():
            setattr(self, "_" + field, value)
        self._free_cache = None
        if self._changed is not None:
            self._changed()

    @property
    def space_sync_bytes(self) -> int:
        """Storage for one GFS space's synced files (owner 2026-11-28): the
        files a household keeps without anyone opening them. 0 = only what
        it opens."""
        return self._space_sync_bytes

    @property
    def space_sync_total_bytes(self) -> int:
        """Storage for the synced files of every GFS space together (owner
        2026-11-28). 0 = no synced files."""
        return self._space_sync_total_bytes

    def set_space_sync_bytes(self, value: int) -> None:
        """Set the synced-files storage in this process only (tests; an
        admin goes through :meth:`update`)."""
        self._space_sync_bytes = int(value) if value > 0 else 0

    @property
    def quota_bytes(self) -> int:
        return self._quota_bytes

    def set_quota_bytes(self, value: int) -> None:
        """Set the cap in this process only (tests; an admin goes through
        :meth:`update`, which keeps it). ``value <= 0`` disables
        enforcement."""
        self._quota_bytes = int(value) if value > 0 else 0
        self._free_cache = None

    async def free_for_sync(self) -> int | None:
        """The quota's free space a synced file may use (``None``: no
        quota). The directory's size may be up to
        :data:`FREE_FOR_SYNC_MAX_AGE_S` old — a sync's headroom check, not
        an upload's."""
        if self._quota_bytes <= 0:
            return None
        now = time.monotonic()
        cached = self._free_cache
        if cached is None or now - cached[0] > FREE_FOR_SYNC_MAX_AGE_S:
            cached = (now, await self.current_usage_bytes())
            self._free_cache = cached
        return max(0, self._quota_bytes - cached[1])

    def note_freed(self, size: int) -> None:
        """A synced file of *size* bytes was removed (Fable B3: the next
        sync sees the room at once, not after the cache ages)."""
        if self._free_cache is not None and size > 0:
            self._free_cache = (
                self._free_cache[0],
                max(0, self._free_cache[1] - size),
            )

    def note_stored(self, size: int) -> None:
        """A synced file of *size* bytes was written: the cached size used
        by :meth:`free_for_sync` grows with it until the next walk."""
        if self._free_cache is not None and size > 0:
            self._free_cache = (self._free_cache[0], self._free_cache[1] + size)

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
        over = used + additional_bytes - self._quota_bytes
        if (
            over > 0
            and additional_bytes <= self._quota_bytes
            and self._evict is not None
        ):
            # Owner 2026-11-28: the household's own upload wins — synced
            # space files make room first.
            try:
                freed = await self._evict(over)
            except Exception:
                log.warning("synced space files not evicted", exc_info=True)
                freed = 0
            if freed > 0:
                self._free_cache = None
                used = await self.current_usage_bytes()
        if used + additional_bytes > self._quota_bytes:
            available = max(0, self._quota_bytes - used)
            raise StorageQuotaExceeded(additional_bytes, available)

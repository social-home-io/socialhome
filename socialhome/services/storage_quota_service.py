"""Storage quota tracking + enforcement (§5.2 ``max_storage_bytes``).

A household has a single global byte budget. The service:

* sums all file_meta.size_bytes across feed_posts + space_posts +
  conversation_messages by walking the persisted JSON fields;
* exposes :meth:`current_usage_bytes` for the GET /api/storage/usage
  endpoint;
* exposes :meth:`check_can_store` which raises
  :class:`StorageQuotaExceeded` when an upload would push the
  household over the configured cap.

The check is best-effort — it's a guard rail, not a security boundary.
A user racing two simultaneous uploads can technically exceed the cap
by a single file's size; that's acceptable for the v1 quota story.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable

from ..repositories.storage_stats_repo import AbstractStorageStatsRepo

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

    __slots__ = ("_repo", "_quota_bytes")

    def __init__(
        self,
        repo: AbstractStorageStatsRepo,
        *,
        quota_bytes: int,
    ) -> None:
        self._repo = repo
        self._quota_bytes = quota_bytes

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
        """Sum the byte size of every file_meta blob in the database."""
        return _sum_meta_sizes(await self._repo.list_file_meta_blobs())

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


# ─── Helpers ─────────────────────────────────────────────────────────────


def _sum_meta_sizes(json_blobs: Iterable[str]) -> int:
    total = 0
    for blob in json_blobs:
        try:
            data = json.loads(blob)
        except TypeError, json.JSONDecodeError:
            continue
        size = data.get("size_bytes") if isinstance(data, dict) else 0
        if isinstance(size, int) and size > 0:
            total += size
    return total

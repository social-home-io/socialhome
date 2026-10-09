"""§25.6 incremental-sync bookkeeping (migration 0086).

* ``sync_seq_counter`` — the per-household monotonic counter the
  ``*_sync_seq_*`` triggers stamp covered rows from. Read-only here:
  nothing in Python writes a stamp.
* ``space_instances.synced_seq`` / ``synced_shape`` / ``synced_full_at`` —
  the provider's watermark for a (space, member household): what that
  household confirmed of our last stream. Lives on the seat row, so it goes
  when the household's last seat does (a rejoin syncs in full).
* ``space_instances.applied_seq`` (migration 0087) — the REQUESTER's echo
  for a (space, provider household): that provider's snapshot of the last
  stream applied cleanly here. Sent as ``have_seq`` in a periodic BEGIN; it
  lives in this database, so it rolls back with the rows it describes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space import SpaceSyncWatermark, parse_have_seq


@runtime_checkable
class AbstractSpaceSyncWatermarkRepo(Protocol):
    async def current_seq(self) -> int:
        """The counter's current value — a stream's snapshot."""
        ...

    async def get(self, space_id: str, instance_id: str) -> SpaceSyncWatermark | None:
        """The household's watermark for the space; ``None`` when it has
        none (never confirmed, or no seat)."""
        ...

    async def confirm(
        self,
        space_id: str,
        instance_id: str,
        *,
        seq: int,
        shape: str,
        full_at: str | None,
    ) -> None:
        """Record a confirmed stream. ``full_at`` ``None`` keeps the time of
        the last full stream (an incremental one). A household without a
        seat in the space gets nothing."""
        ...

    async def applied_seq(self, space_id: str, instance_id: str) -> int | None:
        """The provider ``instance_id``'s snapshot of the last stream applied
        cleanly here for the space; ``None`` when none is recorded."""
        ...

    async def record_applied(self, space_id: str, instance_id: str, seq: int) -> None:
        """Record that a stream from ``instance_id`` up to its snapshot
        ``seq`` applied cleanly here. No seat row for that household: no-op."""
        ...


class SqliteSpaceSyncWatermarkRepo:
    """SQLite-backed :class:`AbstractSpaceSyncWatermarkRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def current_seq(self) -> int:
        row = await self._db.fetchone("SELECT seq FROM sync_seq_counter WHERE id = 1")
        return int(row["seq"]) if row is not None else 0

    async def get(self, space_id: str, instance_id: str) -> SpaceSyncWatermark | None:
        row = await self._db.fetchone(
            "SELECT synced_seq, synced_shape, synced_full_at FROM space_instances"
            " WHERE space_id=? AND instance_id=? AND synced_seq IS NOT NULL",
            (space_id, instance_id),
        )
        if row is None:
            return None
        return SpaceSyncWatermark(
            seq=int(row["synced_seq"]),
            shape=str(row["synced_shape"] or ""),
            full_at=row["synced_full_at"],
        )

    async def confirm(
        self,
        space_id: str,
        instance_id: str,
        *,
        seq: int,
        shape: str,
        full_at: str | None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE space_instances SET synced_seq=?, synced_shape=?,"
            " synced_full_at=COALESCE(?, synced_full_at)"
            " WHERE space_id=? AND instance_id=?",
            (int(seq), shape, full_at, space_id, instance_id),
        )

    async def applied_seq(self, space_id: str, instance_id: str) -> int | None:
        row = await self._db.fetchone(
            "SELECT applied_seq FROM space_instances WHERE space_id=? AND instance_id=?",
            (space_id, instance_id),
        )
        if row is None or row["applied_seq"] is None:
            return None
        return int(row["applied_seq"])

    async def record_applied(self, space_id: str, instance_id: str, seq: int) -> None:
        # Refused here, never in the writer: an out-of-range int raises
        # ``OverflowError`` inside the coalesced batch and fails all of it.
        if parse_have_seq(seq) is None:
            raise ValueError(f"applied seq out of range: {seq!r}")
        await self._db.enqueue(
            "UPDATE space_instances SET applied_seq=?"
            " WHERE space_id=? AND instance_id=?",
            (int(seq), space_id, instance_id),
        )

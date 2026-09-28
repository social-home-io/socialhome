"""Space-post media-blob outbox repository.

Sibling of :class:`SqliteDmMediaOutboxRepo` — same shape, different
FK target (``space_posts`` instead of ``conversation_messages``) and
different scheduler. The two workloads stay separate so:

* the schedulers can backoff independently (a stuck DM peer doesn't
  starve space sends);
* the per-table indices match the read pattern (DM looks up by
  ``message_id`` for deletes; spaces by ``post_id``);
* the federation event types stay distinct (DM_MEDIA_BLOB vs
  SPACE_MEDIA_BLOB) so the inbound handler routes correctly.

See ``socialhome/migrations/0011_space_media_outbox.sql`` for the
schema audit + shape rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import rows_to_dicts


@dataclass(slots=True, frozen=True)
class SpaceMediaOutboxEntry:
    """One pending ``SPACE_MEDIA_BLOB`` send.

    Repo-local DTO — these rows never leave the repository layer in
    raw form; the scheduler consumes them and turns each row into a
    federation envelope (chunked if the file exceeds the single-chunk
    threshold).
    """

    blob_id: str
    #: Soft backref to whatever spawned this row — a ``post_id`` for
    #: space-feed posts, a ``gallery_item_id`` for gallery items.
    #: The scheduler never reads it; the SPA's debug surface uses it.
    correlation_id: str
    space_id: str
    target_instance_id: str
    bytes_path: str
    status: str
    attempts: int
    next_attempt_at: str
    last_error: str | None
    created_at: str


@runtime_checkable
class AbstractSpaceMediaOutboxRepo(Protocol):
    async def enqueue(
        self,
        *,
        blob_id: str,
        space_id: str,
        correlation_id: str,
        target_instance_id: str,
        bytes_path: str,
    ) -> None: ...

    async def list_due(self, *, limit: int = 25) -> list[SpaceMediaOutboxEntry]: ...

    async def mark_in_flight(
        self, *, blob_id: str, target_instance_id: str
    ) -> None: ...

    async def delete(self, *, blob_id: str, target_instance_id: str) -> None: ...

    async def delete_for_instance(self, target_instance_id: str) -> None: ...

    async def reschedule(
        self,
        *,
        blob_id: str,
        target_instance_id: str,
        attempts: int,
        next_attempt_at: str,
        last_error: str | None,
    ) -> None: ...

    async def mark_failed(
        self,
        *,
        blob_id: str,
        target_instance_id: str,
        last_error: str | None,
    ) -> None: ...

    async def list_for_correlation(
        self, correlation_id: str
    ) -> list[SpaceMediaOutboxEntry]: ...

    async def reclaim_in_flight(self) -> int: ...


class SqliteSpaceMediaOutboxRepo:
    """SQLite-backed :class:`AbstractSpaceMediaOutboxRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def enqueue(
        self,
        *,
        blob_id: str,
        space_id: str,
        correlation_id: str,
        target_instance_id: str,
        bytes_path: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_media_outbox(
                blob_id, space_id, correlation_id, target_instance_id, bytes_path
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(blob_id, target_instance_id) DO NOTHING
            """,
            (
                blob_id,
                space_id,
                correlation_id,
                target_instance_id,
                bytes_path,
            ),
        )

    async def list_due(self, *, limit: int = 25) -> list[SpaceMediaOutboxEntry]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM space_media_outbox
             WHERE status='pending'
               AND datetime(next_attempt_at) <= datetime('now')
             ORDER BY datetime(next_attempt_at) ASC, created_at ASC
             LIMIT ?
            """,
            (int(limit),),
        )
        return [_row_to_entry(d) for d in rows_to_dicts(rows)]

    async def mark_in_flight(self, *, blob_id: str, target_instance_id: str) -> None:
        await self._db.enqueue(
            "UPDATE space_media_outbox SET status='in_flight' "
            "WHERE blob_id=? AND target_instance_id=?",
            (blob_id, target_instance_id),
        )

    async def delete(self, *, blob_id: str, target_instance_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_media_outbox WHERE blob_id=? AND target_instance_id=?",
            (blob_id, target_instance_id),
        )

    async def delete_for_instance(self, target_instance_id: str) -> None:
        """Delete every row (any status) addressed to one household.

        Called when that household becomes an unpair tombstone: it gets
        our ``UNPAIR`` and nothing else, so its media never ships.
        Mirrors ``SqliteOutboxRepo.delete_for_instance``.
        """
        await self._db.enqueue(
            "DELETE FROM space_media_outbox WHERE target_instance_id=?",
            (target_instance_id,),
        )

    async def reschedule(
        self,
        *,
        blob_id: str,
        target_instance_id: str,
        attempts: int,
        next_attempt_at: str,
        last_error: str | None,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE space_media_outbox
               SET status='pending',
                   attempts=?,
                   next_attempt_at=?,
                   last_error=?
             WHERE blob_id=? AND target_instance_id=?
            """,
            (
                int(attempts),
                next_attempt_at,
                last_error,
                blob_id,
                target_instance_id,
            ),
        )

    async def mark_failed(
        self,
        *,
        blob_id: str,
        target_instance_id: str,
        last_error: str | None,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE space_media_outbox
               SET status='failed',
                   last_error=?
             WHERE blob_id=? AND target_instance_id=?
            """,
            (last_error, blob_id, target_instance_id),
        )

    async def list_for_correlation(
        self, correlation_id: str
    ) -> list[SpaceMediaOutboxEntry]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_media_outbox WHERE correlation_id=?",
            (correlation_id,),
        )
        return [_row_to_entry(d) for d in rows_to_dicts(rows)]

    async def reclaim_in_flight(self) -> int:
        """Flip orphaned ``in_flight`` rows back to ``pending`` on boot."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM space_media_outbox WHERE status='in_flight'",
            (),
        )
        stuck = int(row["n"] if row is not None else 0)
        if stuck:
            await self._db.enqueue(
                """
                UPDATE space_media_outbox
                   SET status='pending',
                       next_attempt_at=datetime('now', '+10 seconds')
                 WHERE status='in_flight'
                """,
                (),
            )
        return stuck


def _row_to_entry(row: dict) -> SpaceMediaOutboxEntry:
    return SpaceMediaOutboxEntry(
        blob_id=row["blob_id"],
        correlation_id=row["correlation_id"],
        space_id=row["space_id"],
        target_instance_id=row["target_instance_id"],
        bytes_path=row["bytes_path"],
        status=row["status"],
        attempts=int(row.get("attempts", 0) or 0),
        next_attempt_at=row["next_attempt_at"],
        last_error=row.get("last_error"),
        created_at=row.get("created_at", ""),
    )

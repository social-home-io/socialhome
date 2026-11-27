"""The durable queue of a GFS space's content items (migration 0158).

One row per (item, planned GFS) — see the migration and
:mod:`socialhome.domain.space_item_outbox`. The service
(:mod:`socialhome.services.space_item_outbox`) owns the retry loop; this
module only reads and writes rows.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_item_outbox import (
    SPACE_QUEUE_MAX_BYTES,
    SPACE_QUEUE_MAX_ITEMS,
    UNPLANNED_GFS,
    UNSENT_QUEUE_FULL,
    QueuedSpaceItem,
    UnsentItem,
)
from .base import rows_to_dicts

_COLS = (
    "item_id, gfs_id, space_id, epoch, ring_class, event_type, author_user_id,"
    " supersede_key, payload, attempts, next_attempt_at, status, error, created_at"
)


def _from_row(row: dict) -> QueuedSpaceItem:
    return QueuedSpaceItem(
        item_id=row["item_id"],
        gfs_id=row["gfs_id"],
        space_id=row["space_id"],
        epoch=int(row["epoch"]),
        ring_class=row["ring_class"],
        event_type=row["event_type"],
        author_user_id=row["author_user_id"],
        supersede_key=row["supersede_key"],
        payload=row["payload"],
        attempts=int(row["attempts"]),
        next_attempt_at=row["next_attempt_at"],
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
    )


def _insert_params(r: QueuedSpaceItem) -> tuple:
    return (
        r.item_id,
        r.gfs_id,
        r.space_id,
        r.epoch,
        r.ring_class,
        r.event_type,
        r.author_user_id,
        r.supersede_key,
        r.payload,
        r.bytes,
        r.attempts,
        r.next_attempt_at,
        r.status,
        r.error,
        r.created_at,
    )


_INSERT = (
    "INSERT OR REPLACE INTO space_item_outbox(item_id, gfs_id, space_id, epoch,"
    " ring_class, event_type, author_user_id, supersede_key, payload, bytes,"
    " attempts, next_attempt_at, status, error, created_at)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


@runtime_checkable
class AbstractSpaceItemOutboxRepo(Protocol):
    async def add(
        self,
        rows: list[QueuedSpaceItem],
        *,
        max_items: int = SPACE_QUEUE_MAX_ITEMS,
        max_bytes: int = SPACE_QUEUE_MAX_BYTES,
    ) -> bool: ...

    async def usage(self, space_id: str) -> tuple[int, int]: ...

    async def list_due(
        self, now: str, *, limit: int = 100
    ) -> list[QueuedSpaceItem]: ...

    async def next_due_at(self) -> str | None: ...

    async def delete(self, item_id: str, gfs_id: str) -> None: ...

    async def reschedule(
        self, item_id: str, gfs_id: str, *, attempts: int, next_attempt_at: str
    ) -> None: ...

    async def defer_gfs(self, gfs_id: str, *, until: str) -> None: ...

    async def expedite(self, gfs_id: str, *, now: str) -> None: ...

    async def expand(
        self, row: QueuedSpaceItem, gfs_ids: list[str], *, now: str
    ) -> None: ...

    async def mark_failed(self, item_id: str, gfs_id: str, error: str) -> None: ...

    async def record_failure(
        self,
        *,
        space_id: str,
        event_type: str,
        author_user_id: str,
        error: str,
        now: str,
        item_id: str | None = None,
    ) -> None: ...

    async def list_failed(
        self, space_id: str, author_user_id: str
    ) -> list[UnsentItem]: ...

    async def retry_failed(
        self, space_id: str, author_user_id: str, *, now: str
    ) -> int: ...

    async def discard_failed(self, space_id: str, author_user_id: str) -> int: ...

    async def count_pending(self, space_id: str) -> int: ...


class SqliteSpaceItemOutboxRepo:
    """SQLite-backed :class:`AbstractSpaceItemOutboxRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def add(
        self,
        rows: list[QueuedSpaceItem],
        *,
        max_items: int = SPACE_QUEUE_MAX_ITEMS,
        max_bytes: int = SPACE_QUEUE_MAX_BYTES,
    ) -> bool:
        """Insert *rows* (one item, one row per GFS) — first dropping the
        waiting rows a latest-only key replaces — unless the space would
        then hold more than *max_items* rows or *max_bytes*: then nothing
        changes and ``False``. One transaction, so two writers can't both
        squeeze past the bound."""
        if not rows:
            return True
        space_id = rows[0].space_id
        key = rows[0].supersede_key

        def _run(conn: sqlite3.Connection) -> bool:
            if key is not None:
                conn.execute(
                    "DELETE FROM space_item_outbox WHERE space_id=?"
                    " AND supersede_key=? AND status='pending'",
                    (space_id, key),
                )
            count, size = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM space_item_outbox"
                " WHERE space_id=?",
                (space_id,),
            ).fetchone()
            if (
                count + len(rows) > max_items
                or size + sum(r.bytes for r in rows) > max_bytes
            ):
                raise _Full
            conn.executemany(_INSERT, [_insert_params(r) for r in rows])
            return True

        try:
            return bool(await self._db.transact(_run))
        except _Full:
            return False

    async def usage(self, space_id: str) -> tuple[int, int]:
        row = await self._db.fetchone(
            "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM space_item_outbox"
            " WHERE space_id=?",
            (space_id,),
        )
        return (int(row[0]), int(row[1])) if row else (0, 0)

    async def list_due(self, now: str, *, limit: int = 100) -> list[QueuedSpaceItem]:
        rows = await self._db.fetchall(
            f"SELECT {_COLS} FROM space_item_outbox"
            " WHERE status='pending' AND next_attempt_at<=?"
            " ORDER BY next_attempt_at, rowid LIMIT ?",
            (now, int(limit)),
        )
        return [_from_row(r) for r in rows_to_dicts(rows)]

    async def next_due_at(self) -> str | None:
        return await self._db.fetchval(
            "SELECT MIN(next_attempt_at) FROM space_item_outbox WHERE status='pending'"
        )

    async def delete(self, item_id: str, gfs_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_item_outbox WHERE item_id=? AND gfs_id=?",
            (item_id, gfs_id),
        )

    async def reschedule(
        self, item_id: str, gfs_id: str, *, attempts: int, next_attempt_at: str
    ) -> None:
        await self._db.enqueue(
            "UPDATE space_item_outbox SET attempts=?, next_attempt_at=?"
            " WHERE item_id=? AND gfs_id=? AND status='pending'",
            (int(attempts), next_attempt_at, item_id, gfs_id),
        )

    async def defer_gfs(self, gfs_id: str, *, until: str) -> None:
        """Every waiting row of *gfs_id* due before *until* waits till then
        (a GFS that is down or asked us to slow down answers them all the
        same)."""
        await self._db.enqueue(
            "UPDATE space_item_outbox SET next_attempt_at=?"
            " WHERE gfs_id=? AND status='pending' AND next_attempt_at<?",
            (until, gfs_id, until),
        )

    async def expedite(self, gfs_id: str, *, now: str) -> None:
        """*gfs_id* is back: what waits for it is due now (and what waits
        for a plan — the reconnect may have brought the grant)."""
        await self._db.enqueue(
            "UPDATE space_item_outbox SET next_attempt_at=?"
            " WHERE gfs_id IN (?, ?) AND status='pending' AND next_attempt_at>?",
            (now, gfs_id, UNPLANNED_GFS, now),
        )

    async def expand(
        self, row: QueuedSpaceItem, gfs_ids: list[str], *, now: str
    ) -> None:
        """An unplanned row became one row per planned GFS, due now."""

        def _run(conn: sqlite3.Connection) -> None:
            gone = conn.execute(
                "DELETE FROM space_item_outbox WHERE item_id=? AND gfs_id=?"
                " AND status='pending'",
                (row.item_id, row.gfs_id),
            ).rowcount
            if not gone:
                return  # superseded or discarded meanwhile
            conn.executemany(
                _INSERT,
                [
                    _insert_params(
                        QueuedSpaceItem(
                            item_id=row.item_id,
                            gfs_id=g,
                            space_id=row.space_id,
                            epoch=row.epoch,
                            ring_class=row.ring_class,
                            event_type=row.event_type,
                            author_user_id=row.author_user_id,
                            supersede_key=row.supersede_key,
                            payload=row.payload,
                            next_attempt_at=now,
                            created_at=row.created_at,
                        )
                    )
                    for g in dict.fromkeys(gfs_ids)
                ],
            )

        await self._db.transact(_run)

    async def mark_failed(self, item_id: str, gfs_id: str, error: str) -> None:
        await self._db.enqueue(
            "UPDATE space_item_outbox SET status='failed', error=?"
            " WHERE item_id=? AND gfs_id=?",
            (error, item_id, gfs_id),
        )

    async def record_failure(
        self,
        *,
        space_id: str,
        event_type: str,
        author_user_id: str,
        error: str,
        now: str,
        item_id: str | None = None,
    ) -> None:
        """A failed item that kept no bytes. A full queue counts its
        refusals on ONE row per (space, author), so the record of a full
        queue can never grow without bound itself."""
        if error == UNSENT_QUEUE_FULL:
            rid = f"full:{author_user_id}"
        else:
            rid = item_id or f"{error}:{uuid.uuid4().hex}"
        await self._db.enqueue(
            "INSERT INTO space_item_outbox(item_id, gfs_id, space_id, epoch,"
            " ring_class, event_type, author_user_id, payload, bytes, attempts,"
            " next_attempt_at, status, error, created_at)"
            " VALUES(?, '', ?, 0, 'durable', ?, ?, NULL, 0, 1, ?, 'failed', ?, ?)"
            " ON CONFLICT(item_id, gfs_id) DO UPDATE SET"
            " attempts=space_item_outbox.attempts+1,"
            " event_type=excluded.event_type, created_at=excluded.created_at",
            (rid, space_id, event_type, author_user_id, now, error, now),
        )

    async def list_failed(self, space_id: str, author_user_id: str) -> list[UnsentItem]:
        rows = await self._db.fetchall(
            "SELECT item_id, MIN(event_type) AS event_type, MIN(error) AS error,"
            " MIN(created_at) AS created_at, MAX(payload IS NOT NULL) AS retryable,"
            " MAX(attempts) AS attempts, MAX(payload IS NULL) AS bare"
            " FROM space_item_outbox WHERE space_id=? AND author_user_id=?"
            " AND status='failed' GROUP BY item_id ORDER BY MIN(created_at), item_id",
            (space_id, author_user_id),
        )
        return [
            UnsentItem(
                item_id=r["item_id"],
                event_type=r["event_type"],
                error=r["error"] or "",
                created_at=r["created_at"],
                retryable=bool(r["retryable"]),
                count=int(r["attempts"]) if r["error"] == UNSENT_QUEUE_FULL else 1,
            )
            for r in rows_to_dicts(rows)
        ]

    async def retry_failed(
        self, space_id: str, author_user_id: str, *, now: str
    ) -> int:
        return await self._db.enqueue_rowcount(
            "UPDATE space_item_outbox SET status='pending', error=NULL,"
            " attempts=0, next_attempt_at=?"
            " WHERE space_id=? AND author_user_id=? AND status='failed'"
            " AND payload IS NOT NULL",
            (now, space_id, author_user_id),
        )

    async def discard_failed(self, space_id: str, author_user_id: str) -> int:
        return await self._db.enqueue_rowcount(
            "DELETE FROM space_item_outbox"
            " WHERE space_id=? AND author_user_id=? AND status='failed'",
            (space_id, author_user_id),
        )

    async def count_pending(self, space_id: str) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(DISTINCT item_id) FROM space_item_outbox"
                " WHERE space_id=? AND status='pending'",
                (space_id,),
                default=0,
            )
        )


class _Full(Exception):
    """The space's queue is full (rolls the transaction back)."""

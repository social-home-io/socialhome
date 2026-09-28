"""DM media-blob outbox repository.

Separate from the main :mod:`federation_outbox` because the workload
is fundamentally different:

* The general federation outbox stores **envelopes** (signed JSON
  payloads ≤ a few kB) and retries them as-is. Embedding multi-MB
  picture / video bytes inline would bloat that table and make every
  unrelated retry-scan slower.
* This outbox stores **a path on disk** to the full-quality media,
  plus the target peer's id. The scheduler reads the file lazily,
  encrypts it with the conversation key, and ships it as a
  :data:`FederationEventType.DM_MEDIA_BLOB` event through the
  *normal* federation outbox — so resilient retry on the wire layer
  reuses the existing machinery. This table tracks the *higher-up*
  state: "have we built and dispatched the blob payload yet?".

Lifecycle:

* ``enqueue`` — :class:`DmService` writes one row per remote peer
  immediately after the matching ``DM_MESSAGE`` lands.
* ``list_due`` — :class:`DmMediaSyncService`'s scheduler picks
  ``status='pending'`` rows whose ``next_attempt_at`` is due.
* ``mark_in_flight`` — claim the row before reading the file so a
  second concurrent scheduler tick can't double-encode the same
  blob.
* ``delete`` — after a successful enqueue into the federation
  outbox, the row goes away (the federation outbox owns retry from
  that point).
* ``reschedule`` — bump ``attempts`` + push ``next_attempt_at`` out
  via exponential backoff when build / encrypt fails (file
  vanished, peer key not available yet).
* ``mark_failed`` — terminal state after the retry budget is
  exhausted. The :class:`ConversationMessage` row's
  ``media_sync_status`` flips to ``'failed'`` so the sender's
  bubble can show a footnote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import rows_to_dicts


@dataclass(slots=True, frozen=True)
class DmMediaOutboxEntry:
    """One pending ``DM_MEDIA_BLOB`` send.

    Repo-local DTO — these rows never leave the repository layer in
    raw form; the scheduler consumes them and turns each row into a
    federation envelope.
    """

    blob_id: str
    message_id: str
    target_instance_id: str
    bytes_path: str
    status: str
    attempts: int
    next_attempt_at: str
    last_error: str | None
    created_at: str


@runtime_checkable
class AbstractDmMediaOutboxRepo(Protocol):
    async def enqueue(
        self,
        *,
        blob_id: str,
        message_id: str,
        target_instance_id: str,
        bytes_path: str,
    ) -> None: ...

    async def list_due(self, *, limit: int = 25) -> list[DmMediaOutboxEntry]: ...

    async def mark_in_flight(
        self,
        *,
        blob_id: str,
        target_instance_id: str,
    ) -> None: ...

    async def delete(
        self,
        *,
        blob_id: str,
        target_instance_id: str,
    ) -> None: ...

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

    async def list_for_message(
        self,
        message_id: str,
    ) -> list[DmMediaOutboxEntry]: ...

    async def reclaim_in_flight(self) -> int: ...


class SqliteDmMediaOutboxRepo:
    """SQLite-backed :class:`AbstractDmMediaOutboxRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def enqueue(
        self,
        *,
        blob_id: str,
        message_id: str,
        target_instance_id: str,
        bytes_path: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO dm_media_outbox(
                blob_id, message_id, target_instance_id, bytes_path
            ) VALUES(?,?,?,?)
            ON CONFLICT(blob_id, target_instance_id) DO NOTHING
            """,
            (blob_id, message_id, target_instance_id, bytes_path),
        )

    async def list_due(self, *, limit: int = 25) -> list[DmMediaOutboxEntry]:
        """Pending rows whose ``next_attempt_at`` is due now.

        Excludes ``in_flight`` rows so a slow scheduler tick can't
        re-pick a row another tick is already encoding. Ordering is
        FIFO within the due-now set so older sends ship first — a
        large queue from a multi-recipient group DM doesn't starve
        a single 1:1 send that landed later.
        """
        rows = await self._db.fetchall(
            """
            SELECT * FROM dm_media_outbox
             WHERE status='pending'
               AND datetime(next_attempt_at) <= datetime('now')
             ORDER BY datetime(next_attempt_at) ASC, created_at ASC
             LIMIT ?
            """,
            (int(limit),),
        )
        return [_row_to_entry(d) for d in rows_to_dicts(rows)]

    async def mark_in_flight(
        self,
        *,
        blob_id: str,
        target_instance_id: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE dm_media_outbox SET status='in_flight' "
            "WHERE blob_id=? AND target_instance_id=?",
            (blob_id, target_instance_id),
        )

    async def delete(
        self,
        *,
        blob_id: str,
        target_instance_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM dm_media_outbox WHERE blob_id=? AND target_instance_id=?",
            (blob_id, target_instance_id),
        )

    async def delete_for_instance(self, target_instance_id: str) -> None:
        """Delete every row (any status) addressed to one household.

        Called when that household becomes an unpair tombstone: it gets
        our ``UNPAIR`` and nothing else, so its media never ships.
        Mirrors ``SqliteOutboxRepo.delete_for_instance``.
        """
        await self._db.enqueue(
            "DELETE FROM dm_media_outbox WHERE target_instance_id=?",
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
            UPDATE dm_media_outbox
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
            UPDATE dm_media_outbox
               SET status='failed',
                   last_error=?
             WHERE blob_id=? AND target_instance_id=?
            """,
            (last_error, blob_id, target_instance_id),
        )

    async def list_for_message(
        self,
        message_id: str,
    ) -> list[DmMediaOutboxEntry]:
        """All outstanding outbox rows for ``message_id``.

        Used by :class:`DmService.soft_delete_message` to drop any
        pending sends — once the message is deleted the recipients
        shouldn't receive a blob that no longer has a backing row.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM dm_media_outbox WHERE message_id=?",
            (message_id,),
        )
        return [_row_to_entry(d) for d in rows_to_dicts(rows)]

    async def reclaim_in_flight(self) -> int:
        """Flip orphaned ``in_flight`` rows back to ``pending`` on boot.

        A sender that crashes between ``mark_in_flight`` and the
        ``send_event`` call (or between successive chunk sends)
        leaves the row stuck at ``in_flight`` — :meth:`list_due`
        filters those out, so the row never retries. Called once
        from :meth:`DmMediaSyncService.start`. The reset pushes
        ``next_attempt_at`` ten seconds out so the scheduler doesn't
        immediately stampede the federation outbox on a busy
        restart.

        Returns the count of rows that were ``in_flight`` before the
        update (read first, then update — the small race is fine for
        a log-line). Returns 0 when there's nothing stuck.
        """
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM dm_media_outbox WHERE status='in_flight'",
            (),
        )
        stuck = int(row["n"] if row is not None else 0)
        if stuck:
            await self._db.enqueue(
                """
                UPDATE dm_media_outbox
                   SET status='pending',
                       next_attempt_at=datetime('now', '+10 seconds')
                 WHERE status='in_flight'
                """,
                (),
            )
        return stuck


def _row_to_entry(row: dict) -> DmMediaOutboxEntry:
    return DmMediaOutboxEntry(
        blob_id=row["blob_id"],
        message_id=row["message_id"],
        target_instance_id=row["target_instance_id"],
        bytes_path=row["bytes_path"],
        status=row["status"],
        attempts=int(row.get("attempts", 0) or 0),
        next_attempt_at=row["next_attempt_at"],
        last_error=row.get("last_error"),
        created_at=row.get("created_at", ""),
    )

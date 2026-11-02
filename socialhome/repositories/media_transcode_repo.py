"""Async video-transcode outbox repository.

Backs the background conversion of the previously-synchronous
upload-time video transcode. One row per uploaded video, keyed by the
eventual output filename (the canonical media key the rest of the app
stores and serves).

Lifecycle:

* ``enqueue`` — the upload handler writes one row immediately after
  stashing the source bytes on disk; the response returns the future
  ``output_filename`` so the SPA can render a "processing" placeholder.
* ``list_due`` — the transcode scheduler picks ``status='pending'``
  rows whose ``next_attempt_at`` is due.
* ``mark_processing`` — claim the row before decoding so a second
  scheduler tick can't double-transcode the same source.
* ``complete`` — on success the row is DELETED; readiness is "absent
  row" (mirrors ``dm_media_outbox.delete`` on success).
* ``reschedule`` — bump ``attempts`` + push ``next_attempt_at`` out via
  the scheduler's backoff when a transcode attempt fails transiently.
* ``mark_failed`` — terminal state after the retry budget is spent;
  ``status_for`` then reports ``'failed'`` so the SPA shows a footnote.
* ``reclaim`` — flip orphaned ``processing`` rows back to ``pending``
  on boot (crash recovery).
* ``status_for`` — bulk readiness lookup for a set of output filenames;
  absent rows mean "ready" and are simply omitted from the result.
* ``list_queue`` / ``count_queued`` — the jobs still to convert, in the
  order they will run, for the time estimate and the queue bound.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.media_transcode import MediaTranscodeJob
from .base import rows_to_dicts


@runtime_checkable
class AbstractMediaTranscodeRepo(Protocol):
    async def enqueue(
        self,
        *,
        output_filename: str,
        source_path: str,
        thumbnail_filename: str,
        owner_user_id: str | None,
        kind: str = "video",
        duration_s: float | None = None,
    ) -> None: ...

    async def list_due(self, *, limit: int = 10) -> list[MediaTranscodeJob]: ...

    async def list_queue(self, *, limit: int = 500) -> list[MediaTranscodeJob]: ...

    async def count_queued(self) -> int: ...

    async def mark_processing(self, output_filename: str) -> None: ...

    async def complete(self, output_filename: str) -> None: ...

    async def reschedule(
        self,
        output_filename: str,
        *,
        attempts: int,
        next_attempt_at: str,
        last_error: str | None,
    ) -> None: ...

    async def mark_failed(
        self,
        output_filename: str,
        last_error: str | None,
    ) -> None: ...

    async def reclaim(self) -> int: ...

    async def status_for(self, output_filenames: list[str]) -> dict[str, str]: ...

    async def active_source_paths(self) -> set[str]: ...


class SqliteMediaTranscodeRepo:
    """SQLite-backed :class:`AbstractMediaTranscodeRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def enqueue(
        self,
        *,
        output_filename: str,
        source_path: str,
        thumbnail_filename: str,
        owner_user_id: str | None,
        kind: str = "video",
        duration_s: float | None = None,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO media_transcode_jobs(
                output_filename, source_path, thumbnail_filename,
                kind, owner_user_id, duration_s
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(output_filename) DO NOTHING
            """,
            (
                output_filename,
                source_path,
                thumbnail_filename,
                kind,
                owner_user_id,
                duration_s,
            ),
        )

    async def list_due(self, *, limit: int = 10) -> list[MediaTranscodeJob]:
        """Pending rows whose ``next_attempt_at`` is due now.

        Excludes ``processing`` / ``failed`` rows so a slow scheduler
        tick can't re-pick a row another tick already claimed. FIFO
        within the due-now set so older uploads transcode first.
        """
        rows = await self._db.fetchall(
            """
            SELECT * FROM media_transcode_jobs
             WHERE status='pending'
               AND datetime(next_attempt_at) <= datetime('now')
             ORDER BY datetime(next_attempt_at) ASC, created_at ASC
             LIMIT ?
            """,
            (int(limit),),
        )
        return [_row_to_job(d) for d in rows_to_dicts(rows)]

    async def list_queue(self, *, limit: int = 500) -> list[MediaTranscodeJob]:
        """Every job still to convert, in the order it will run.

        The one being converted first, then the waiting ones the way
        :meth:`list_due` picks them (a retry waiting for its back-off
        sorts by its next attempt). ``failed`` rows never run again and
        are left out.
        """
        rows = await self._db.fetchall(
            """
            SELECT * FROM media_transcode_jobs
             WHERE status IN ('pending', 'processing')
             ORDER BY status='processing' DESC,
                      datetime(next_attempt_at) ASC, created_at ASC
             LIMIT ?
            """,
            (int(limit),),
        )
        return [_row_to_job(d) for d in rows_to_dicts(rows)]

    async def count_queued(self) -> int:
        """Jobs waiting or being converted (``failed`` ones don't count)."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM media_transcode_jobs "
            "WHERE status IN ('pending', 'processing')",
            (),
        )
        return int(row["n"] if row is not None else 0)

    async def mark_processing(self, output_filename: str) -> None:
        await self._db.enqueue(
            "UPDATE media_transcode_jobs SET status='processing' "
            "WHERE output_filename=?",
            (output_filename,),
        )

    async def complete(self, output_filename: str) -> None:
        await self._db.enqueue(
            "DELETE FROM media_transcode_jobs WHERE output_filename=?",
            (output_filename,),
        )

    async def reschedule(
        self,
        output_filename: str,
        *,
        attempts: int,
        next_attempt_at: str,
        last_error: str | None,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE media_transcode_jobs
               SET status='pending',
                   attempts=?,
                   next_attempt_at=?,
                   last_error=?
             WHERE output_filename=?
            """,
            (int(attempts), next_attempt_at, last_error, output_filename),
        )

    async def mark_failed(
        self,
        output_filename: str,
        last_error: str | None,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE media_transcode_jobs
               SET status='failed',
                   last_error=?
             WHERE output_filename=?
            """,
            (last_error, output_filename),
        )

    async def reclaim(self) -> int:
        """Flip orphaned ``processing`` rows back to ``pending`` on boot.

        A worker that crashes between ``mark_processing`` and
        ``complete`` leaves the row stuck at ``processing`` —
        :meth:`list_due` filters those out, so it never retries.
        Called once from the scheduler's ``start``. Returns the count
        of rows that were ``processing`` before the reset (read first,
        then update — the small race is fine for a log line).
        """
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM media_transcode_jobs WHERE status='processing'",
            (),
        )
        stuck = int(row["n"] if row is not None else 0)
        if stuck:
            await self._db.enqueue(
                "UPDATE media_transcode_jobs SET status='pending' "
                "WHERE status='processing'",
                (),
            )
        return stuck

    async def status_for(self, output_filenames: list[str]) -> dict[str, str]:
        """Bulk readiness lookup keyed by output filename.

        Returns ``{filename: 'processing'}`` for pending / processing
        rows and ``{filename: 'failed'}`` for failed rows. Filenames
        with no row are omitted — the caller treats absent as ``ready``.
        """
        if not output_filenames:
            return {}
        placeholders = ",".join("?" * len(output_filenames))
        rows = await self._db.fetchall(
            "SELECT output_filename, status FROM media_transcode_jobs "
            f"WHERE output_filename IN ({placeholders})",
            tuple(output_filenames),
        )
        out: dict[str, str] = {}
        for d in rows_to_dicts(rows):
            status = d["status"]
            out[d["output_filename"]] = "failed" if status == "failed" else "processing"
        return out

    async def active_source_paths(self) -> set[str]:
        """Every ``source_path`` still referenced by a job row.

        Any row (regardless of status) means its on-disk source temp
        file is still in use — the orphan sweep keeps those and only
        reaps ``transcode_src`` blobs no row references.
        """
        rows = await self._db.fetchall(
            "SELECT source_path FROM media_transcode_jobs",
            (),
        )
        return {d["source_path"] for d in rows_to_dicts(rows)}


def _row_to_job(row: dict) -> MediaTranscodeJob:
    return MediaTranscodeJob(
        output_filename=row["output_filename"],
        source_path=row["source_path"],
        thumbnail_filename=row["thumbnail_filename"],
        kind=row.get("kind", "video"),
        owner_user_id=row.get("owner_user_id"),
        status=row["status"],
        attempts=int(row.get("attempts", 0) or 0),
        next_attempt_at=row["next_attempt_at"],
        last_error=row.get("last_error"),
        created_at=row.get("created_at"),
        duration_s=(
            float(row["duration_s"]) if row.get("duration_s") is not None else None
        ),
    )

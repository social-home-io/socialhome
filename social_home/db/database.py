"""AsyncDatabase — WAL-mode SQLite with a coalescing write queue (§28.3).

Reads go straight to a dedicated asyncio-friendly connection (``fetchone`` /
``fetchall``). Writes are queued and dispatched to a single writer
coroutine that coalesces multiple statements into one transaction per tick,
bounded by ``db_write_batch_max`` statements and
``db_write_batch_timeout_ms`` time. This keeps SQLite happy (one writer at a
time) without stalling callers on disk fsyncs.

Rows come back as :class:`sqlite3.Row` so both index and key access work.
Schema migrations are applied *synchronously* on :meth:`startup` — before any
request handler runs.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .migrations import MIGRATIONS_DIR, run_migrations

log = logging.getLogger(__name__)


@dataclass
class _PendingWrite:
    sql: str
    params: tuple[Any, ...]
    future: "asyncio.Future[int]"  # resolves to cursor.lastrowid


class AsyncDatabase:
    """An asyncio-friendly SQLite wrapper using WAL + coalesced writes.

    Call :meth:`startup` before use and :meth:`shutdown` on teardown.

    Connection flags applied on open:

    * ``PRAGMA journal_mode=WAL`` — lets readers and writers coexist.
    * ``PRAGMA synchronous=NORMAL`` — durability / throughput compromise.
    * ``PRAGMA cache_size=-16384`` — 16 MiB of page cache.
    * ``PRAGMA mmap_size=134217728`` — 128 MiB memory-mapped read window.
    * ``PRAGMA foreign_keys=ON`` — enforce referential integrity.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        batch_max: int = 50,
        batch_timeout_ms: int = 500,
        migrations_dir: Path | None = None,
    ) -> None:
        self._path = str(path)
        self._batch_max = batch_max
        self._batch_timeout = batch_timeout_ms / 1000.0
        self._migrations_dir = migrations_dir or MIGRATIONS_DIR

        # Populated by ``startup()``.
        self._conn: sqlite3.Connection | None = None
        self._write_queue: asyncio.Queue[_PendingWrite] | None = None
        self._writer_task: asyncio.Task | None = None
        # Serialises all write transactions (queue batches + transact() calls)
        # so the single shared sqlite3 connection never sees nested BEGINs.
        self._writer_lock: asyncio.Lock | None = None
        self._closed = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Open the database, apply pending migrations, start the writer."""
        if self._conn is not None:
            return

        loop = asyncio.get_running_loop()

        # Open a blocking connection inside the executor (sqlite3.connect
        # can do a brief I/O stall on first WAL setup) and configure it.
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(
                self._path,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-16384")
            conn.execute("PRAGMA mmap_size=134217728")
            conn.execute("PRAGMA foreign_keys=ON")
            run_migrations(conn, directory=self._migrations_dir)
            return conn

        self._conn = await loop.run_in_executor(None, _open)

        self._write_queue = asyncio.Queue()
        self._writer_lock = asyncio.Lock()
        self._writer_task = loop.create_task(
            self._writer_loop(),
            name="AsyncDatabase-writer",
        )

    async def shutdown(self) -> None:
        """Drain pending writes and close the underlying connection."""
        if self._closed:
            return
        self._closed = True
        if self._writer_task is not None:
            # Send a sentinel None so the writer exits cleanly after draining.
            assert self._write_queue is not None
            await self._write_queue.put(None)  # type: ignore[arg-type]
            await self._writer_task
            self._writer_task = None
        if self._conn is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._conn.close)
            self._conn = None

    # ── Reads (direct) ───────────────────────────────────────────────────

    async def fetchone(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> sqlite3.Row | None:
        return await self._read(lambda c: c.execute(sql, params).fetchone())

    async def fetchall(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[sqlite3.Row]:
        return await self._read(lambda c: c.execute(sql, params).fetchall())

    async def fetchval(
        self,
        sql: str,
        params: Sequence[Any] = (),
        default: Any = None,
    ) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return default
        return row[0]

    # ── Writes (queued / coalesced) ──────────────────────────────────────

    async def enqueue(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int:
        """Queue a write statement. Returns the resulting ``lastrowid``.

        Awaits the completion of the transaction containing this write, so
        callers that need read-after-write consistency can simply ``await``.
        """
        self._assert_running()
        assert self._write_queue is not None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        await self._write_queue.put(
            _PendingWrite(sql=sql, params=tuple(params), future=fut),
        )
        return await fut

    async def executemany(
        self,
        sql: str,
        seq_of_params: Iterable[Sequence[Any]],
    ) -> None:
        """Convenience for bulk inserts — each row awaits its own write."""
        for row in seq_of_params:
            await self.enqueue(sql, row)

    async def transact(self, fn):
        """Run ``fn(conn)`` inside a ``BEGIN IMMEDIATE`` transaction.

        ``fn`` is a *synchronous* callable — it runs on the DB executor
        thread. Use this when a single logical step must read and write
        atomically (e.g. ``UPDATE foo SET n=n+1; SELECT n FROM foo``) —
        plain ``enqueue()`` cannot express that because reads go through
        the independent read path.

        The return value of ``fn`` is forwarded back to the caller.
        """
        self._assert_running()
        assert self._conn is not None
        assert self._writer_lock is not None
        conn = self._conn  # capture for mypy narrowing
        loop = asyncio.get_running_loop()

        def _run():
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return result

        async with self._writer_lock:
            return await loop.run_in_executor(None, _run)

    async def checkpoint(self, mode: str = "TRUNCATE") -> tuple[int, int, int]:
        """Force a WAL checkpoint.  Required before snapshotting the DB file.

        WAL-mode SQLite keeps freshly-committed pages in the
        ``-wal`` sidecar file until a checkpoint moves them into the
        main DB. A naive file-level snapshot of the main DB therefore
        loses any data still sitting in WAL.

        Calling this method, then quiescing further writes, lets a HA
        Supervisor backup capture a consistent snapshot of ``/data``.

        ``mode`` matches SQLite's ``PRAGMA wal_checkpoint`` argument:

        * ``"PASSIVE"``  — best-effort, may leave WAL frames behind.
        * ``"FULL"``     — block until all readers finish.
        * ``"RESTART"``  — like FULL plus reset the WAL counter.
        * ``"TRUNCATE"`` — like RESTART plus truncate the WAL file
          to zero bytes (the strongest guarantee — recommended).

        Returns SQLite's ``(busy, log_frames, checkpointed_frames)``
        tuple — useful for monitoring how much data was flushed.
        """
        self._assert_running()
        assert self._conn is not None
        assert self._writer_lock is not None
        if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValueError(f"Invalid checkpoint mode: {mode!r}")
        conn = self._conn
        loop = asyncio.get_running_loop()

        def _run():
            cur = conn.execute(f"PRAGMA wal_checkpoint({mode})")
            row = cur.fetchone()
            return tuple(row) if row else (0, 0, 0)

        # Hold the writer lock so no in-flight batch races the checkpoint.
        async with self._writer_lock:
            return await loop.run_in_executor(None, _run)

    # ── Internals ────────────────────────────────────────────────────────

    async def _read(self, fn):
        self._assert_running()
        assert self._conn is not None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, self._conn)

    async def _writer_loop(self) -> None:
        assert self._conn is not None
        assert self._write_queue is not None
        conn = self._conn
        loop = asyncio.get_running_loop()
        while True:
            first = await self._write_queue.get()
            if first is None:  # sentinel — shutdown
                return
            batch: list[_PendingWrite] = [first]
            deadline = loop.time() + self._batch_timeout

            # Greedy draining with a soft cap.
            while len(batch) < self._batch_max:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(
                        self._write_queue.get(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    break
                if nxt is None:
                    # Shutdown requested mid-batch. Finish the batch, then exit.
                    await self._apply_batch(conn, batch)
                    return
                batch.append(nxt)

            await self._apply_batch(conn, batch)

    async def _apply_batch(
        self,
        conn: sqlite3.Connection,
        batch: list[_PendingWrite],
    ) -> None:
        loop = asyncio.get_running_loop()
        assert self._writer_lock is not None

        def _run() -> list[tuple["_PendingWrite", Any]]:
            # Single transaction for the whole batch. If any statement fails,
            # the entire batch rolls back and every future receives the
            # exception — callers see the failure atomically.
            results: list[tuple[_PendingWrite, Any]] = []
            conn.execute("BEGIN IMMEDIATE")
            try:
                for pending in batch:
                    cursor = conn.execute(pending.sql, pending.params)
                    results.append((pending, cursor.lastrowid or 0))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return results

        try:
            async with self._writer_lock:
                results = await loop.run_in_executor(None, _run)
        except Exception as exc:
            for pending in batch:
                if not pending.future.done():
                    pending.future.set_exception(exc)
            log.exception("DB write batch failed (%d stmts)", len(batch))
            return

        for pending, rowid in results:
            if not pending.future.done():
                pending.future.set_result(rowid)

    def _assert_running(self) -> None:
        if self._conn is None:
            raise RuntimeError(
                "AsyncDatabase not started — call await db.startup() first",
            )
        if self._closed:
            raise RuntimeError("AsyncDatabase has been shut down")

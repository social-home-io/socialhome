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
import threading
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
    #: When set, the future resolves to ``cursor.rowcount`` instead of
    #: ``lastrowid``. Scoped mutators (``… WHERE space_id=?``) need to
    #: know whether the predicate actually matched a row so the caller
    #: can refuse + log a cross-space write attempt.
    want_rowcount: bool = False


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
        # Threading-level lock around every operation on the shared
        # ``sqlite3.Connection``. With ``check_same_thread=False`` and
        # ``run_in_executor`` scheduling reads on a thread pool, two
        # concurrent ``conn.execute()`` calls can race the Python wrapper
        # state and surface as ``sqlite3.InterfaceError: bad parameter or
        # other API misuse``. Acquired by ``_read`` and by every
        # ``_run`` body that touches the connection (writer batch,
        # ``transact``, ``checkpoint``).
        self._conn_thread_lock = threading.RLock()
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
            # A migration may legitimately disable FK enforcement to rebuild
            # a table (0046 does), and ``PRAGMA foreign_keys`` is a silent
            # no-op inside a transaction — so the migration's own restore can
            # fail without a sound (it does exactly that on a connection left
            # in legacy implicit-transaction mode). Re-assert on the way out
            # and verify: this connection is the process's long-lived writer,
            # and leaving it with enforcement off would make every runtime
            # ON DELETE CASCADE inert for the whole boot.
            conn.execute("PRAGMA foreign_keys=ON")
            if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError(
                    "Refusing to serve with foreign-key enforcement disabled: "
                    "PRAGMA foreign_keys=ON did not take effect after "
                    "migrations (most likely a transaction is still open). "
                    "Every ON DELETE CASCADE would be inert for this process."
                )
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
            conn = self._conn
            lock = self._conn_thread_lock

            def _locked_close() -> None:
                # Serialised against every in-flight ``_read`` / ``transact``
                # / ``checkpoint`` body: they all touch this same connection
                # from other executor threads under this lock. Closing a
                # ``sqlite3.Connection`` while another thread is inside
                # ``conn.execute(...)`` is a data race down in ``_sqlite3``
                # and faults the whole interpreter — no Python traceback, just
                # a dead process (under xdist: "node down: Not properly
                # terminated"). Taking the lock makes the close wait its turn;
                # a read that arrives after it fails cleanly with
                # ``sqlite3.ProgrammingError: Cannot operate on a closed
                # database`` instead, which a caller can see and log.
                with lock:
                    conn.close()

            await loop.run_in_executor(None, _locked_close)
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

    async def enqueue_rowcount(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int:
        """Queue a write statement. Returns the number of rows it changed.

        Same coalesced-batch path as :meth:`enqueue` — only the value the
        future resolves to differs. Use it for the space-scoped mutators
        (``UPDATE … WHERE id=? AND space_id=?``): a ``0`` return means the
        row exists in a *different* space (or not at all), which callers
        surface as a refusal rather than a silent no-op.
        """
        self._assert_running()
        assert self._write_queue is not None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        await self._write_queue.put(
            _PendingWrite(
                sql=sql,
                params=tuple(params),
                future=fut,
                want_rowcount=True,
            ),
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

        lock = self._conn_thread_lock

        def _run():
            with lock:
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

        lock = self._conn_thread_lock

        def _run():
            with lock:
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
        lock = self._conn_thread_lock

        def _locked(conn):
            with lock:
                return fn(conn)

        return await loop.run_in_executor(None, _locked, self._conn)

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

        lock = self._conn_thread_lock

        def _run() -> list[tuple["_PendingWrite", int | BaseException]]:
            # Each pending statement is wrapped in its own SAVEPOINT so a
            # constraint violation in one handler doesn't 500 every other
            # handler whose statement happened to be drained into the same
            # batch (issue #278). The outer ``BEGIN IMMEDIATE``/``COMMIT``
            # still wraps the batch so the throughput win of coalescing
            # is preserved.
            #
            # Per-statement failures classified as ``sqlite3.Error`` (FK,
            # CHECK, UNIQUE, …) are rolled back to the savepoint, recorded
            # on the future, and the loop continues. Anything else
            # (Python-level bugs, ``MemoryError``, …) propagates out and
            # the outer try/except rolls the whole batch back — those are
            # not "this row was bad", they're "the writer is in an
            # unrecoverable state".
            with lock:
                results: list[tuple[_PendingWrite, int | BaseException]] = []
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for i, pending in enumerate(batch):
                        sp = f"sh_batch_{i}"
                        conn.execute(f"SAVEPOINT {sp}")
                        try:
                            cursor = conn.execute(pending.sql, pending.params)
                        except sqlite3.Error as stmt_exc:
                            # Roll back only this statement; release the
                            # savepoint so it doesn't pin the page cache.
                            conn.execute(f"ROLLBACK TO {sp}")
                            conn.execute(f"RELEASE {sp}")
                            results.append((pending, stmt_exc))
                            continue
                        conn.execute(f"RELEASE {sp}")
                        if pending.want_rowcount:
                            results.append((pending, cursor.rowcount))
                        else:
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

        # Per-statement: succeed or fail each future independently.
        failures = 0
        for pending, outcome in results:
            if pending.future.done():
                continue
            if isinstance(outcome, BaseException):
                pending.future.set_exception(outcome)
                failures += 1
            else:
                pending.future.set_result(outcome)
        if failures:
            log.warning(
                "DB write batch: %d/%d statements failed (rolled back to "
                "per-statement savepoint, sibling statements committed)",
                failures,
                len(batch),
            )

    def _assert_running(self) -> None:
        if self._conn is None:
            raise RuntimeError(
                "AsyncDatabase not started — call await db.startup() first",
            )
        if self._closed:
            raise RuntimeError("AsyncDatabase has been shut down")

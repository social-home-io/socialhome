"""Tests for socialhome.db.database and socialhome.db.migrations."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.db.migrations import MigrationError, discover_migrations


async def test_startup_creates_tables(tmp_dir):
    """startup() runs all migrations; the instance_identity table must exist."""
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    row = await db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='instance_identity'"
    )
    assert row is not None
    await db.shutdown()


async def test_enqueue_and_fetchone(tmp_dir):
    """enqueue writes a row that fetchone can retrieve."""
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    from socialhome.crypto import generate_identity_keypair, derive_instance_id

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    row = await db.fetchone("SELECT instance_id FROM instance_identity WHERE id='self'")
    assert row is not None
    await db.shutdown()


async def test_fetchval_default(tmp_dir):
    """fetchval returns the default when the query produces no row."""
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    val = await db.fetchval(
        "SELECT COUNT(*) FROM users WHERE username='nobody'", default=0
    )
    assert val == 0
    await db.shutdown()


async def test_fetchall_empty(tmp_dir):
    """fetchall on an empty result set returns an empty list."""
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    rows = await db.fetchall("SELECT * FROM users WHERE 1=0")
    assert rows == []
    await db.shutdown()


def test_discover_empty_directory(tmp_dir):
    """Empty migration directory returns no migrations."""
    d = tmp_dir / "empty"
    d.mkdir()
    assert discover_migrations(d) == []


def test_discover_ignores_non_sql_py(tmp_dir):
    """Non .sql/.py files are ignored silently."""
    d = tmp_dir / "mig"
    d.mkdir()
    (d / "README.md").write_text("hi")
    (d / "0001_init.sql").write_text("SELECT 1;")
    ms = discover_migrations(d)
    assert len(ms) == 1


def test_duplicate_version_detected(tmp_dir):
    """Duplicate version numbers raise MigrationError."""
    d = tmp_dir / "mig"
    d.mkdir()
    (d / "0001_a.sql").write_text("SELECT 1;")
    (d / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="Duplicate"):
        discover_migrations(d)


def test_bad_filename_raises(tmp_dir):
    """SQL file with bad naming convention raises MigrationError."""
    d = tmp_dir / "mig"
    d.mkdir()
    (d / "bad_name.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="does not match"):
        discover_migrations(d)


def test_python_migration(tmp_dir):
    """Python migration file is discovered and runnable."""
    d = tmp_dir / "mig"
    d.mkdir()
    (d / "0001_init.py").write_text(
        "def migrate(conn):\n    conn.execute('CREATE TABLE IF NOT EXISTS _test(id INT)')\n"
    )
    ms = discover_migrations(d)
    assert len(ms) == 1 and ms[0].is_python
    conn = sqlite3.connect(":memory:")
    ms[0].apply(conn)
    conn.execute("SELECT * FROM _test")


# ── AsyncDatabase additional paths ────────────────────────────────────────


async def test_transact_atomic(tmp_dir):
    """transact runs a function atomically inside BEGIN IMMEDIATE."""
    db = AsyncDatabase(tmp_dir / "tr.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT)")
    await db.enqueue("INSERT INTO kv VALUES('a','1')")

    def increment(conn):
        row = conn.execute("SELECT v FROM kv WHERE k='a'").fetchone()
        new_val = str(int(row[0]) + 1)
        conn.execute("UPDATE kv SET v=? WHERE k='a'", (new_val,))
        return new_val

    result = await db.transact(increment)
    assert result == "2"
    row = await db.fetchone("SELECT v FROM kv WHERE k='a'")
    assert row[0] == "2"
    await db.shutdown()


# ── checkpoint ────────────────────────────────────────────────────────────


async def test_checkpoint_returns_three_int_tuple(tmp_dir):
    """PRAGMA wal_checkpoint returns (busy, log_frames, ckpt_frames)."""
    db = AsyncDatabase(tmp_dir / "ck.db", batch_timeout_ms=10)
    await db.startup()
    busy, log_frames, ckpt_frames = await db.checkpoint()
    assert isinstance(busy, int) and isinstance(log_frames, int)
    assert isinstance(ckpt_frames, int)
    await db.shutdown()


async def test_checkpoint_truncates_wal_file(tmp_dir):
    """After write + TRUNCATE checkpoint the .db-wal sidecar should shrink."""
    db = AsyncDatabase(tmp_dir / "ck2.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue("CREATE TABLE t(x INTEGER)")
    for i in range(50):
        await db.enqueue("INSERT INTO t VALUES(?)", (i,))
    await db.checkpoint("TRUNCATE")
    wal_path = tmp_dir / "ck2.db-wal"
    if wal_path.exists():
        # TRUNCATE empties the WAL — file may exist but should be 0 bytes.
        assert wal_path.stat().st_size == 0
    await db.shutdown()


async def test_checkpoint_rejects_invalid_mode(tmp_dir):
    db = AsyncDatabase(tmp_dir / "ck3.db", batch_timeout_ms=10)
    await db.startup()
    with pytest.raises(ValueError):
        await db.checkpoint("BOGUS_MODE")
    await db.shutdown()


async def test_checkpoint_concurrent_with_writes_consistent(tmp_dir):
    """checkpoint and concurrent writes both succeed without corruption."""
    import asyncio

    db = AsyncDatabase(tmp_dir / "ck4.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue("CREATE TABLE t(x INTEGER)")

    async def writer():
        for i in range(20):
            await db.enqueue("INSERT INTO t VALUES(?)", (i,))

    async def checkpointer():
        for _ in range(5):
            await db.checkpoint("PASSIVE")

    await asyncio.gather(writer(), checkpointer())
    row = await db.fetchone("SELECT COUNT(*) AS n FROM t")
    assert row["n"] == 20
    await db.shutdown()


async def test_transact_rollback_on_error(tmp_dir):
    """transact rolls back on exception and propagates it."""
    db = AsyncDatabase(tmp_dir / "tr2.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT)")
    await db.enqueue("INSERT INTO kv VALUES('a','1')")

    def fail(conn):
        conn.execute("UPDATE kv SET v='999' WHERE k='a'")
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await db.transact(fail)

    row = await db.fetchone("SELECT v FROM kv WHERE k='a'")
    assert row[0] == "1"  # rolled back
    await db.shutdown()


async def test_fetchval(tmp_dir):
    """fetchval returns the first column of the first row."""
    db = AsyncDatabase(tmp_dir / "fv.db", batch_timeout_ms=10)
    await db.startup()
    assert await db.fetchval("SELECT 42") == 42
    assert await db.fetchval("SELECT NULL WHERE 0", default="x") == "x"
    await db.shutdown()


async def test_shutdown_twice(tmp_dir):
    """Calling shutdown twice is safe."""
    db = AsyncDatabase(tmp_dir / "s2.db", batch_timeout_ms=10)
    await db.startup()
    await db.shutdown()
    await db.shutdown()


async def test_operations_before_startup(tmp_dir):
    """Operations before startup raise RuntimeError."""
    db = AsyncDatabase(tmp_dir / "ns.db", batch_timeout_ms=10)
    with pytest.raises(RuntimeError):
        await db.fetchone("SELECT 1")


async def test_batch_write_error_propagates(tmp_dir):
    """A write batch with a constraint error propagates to the caller."""
    import sqlite3 as _sql

    db = AsyncDatabase(tmp_dir / "be.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue("CREATE TABLE u(id TEXT PRIMARY KEY)")
    await db.enqueue("INSERT INTO u VALUES('a')")
    with pytest.raises(_sql.IntegrityError):
        await db.enqueue("INSERT INTO u VALUES('a')")  # duplicate
    await db.shutdown()


async def test_batch_error_is_isolated_to_failing_statement(tmp_dir):
    """Regression for #278.

    Two independent enqueues that land in the same writer batch must
    not poison each other: the failing statement raises in its own
    awaiter and rolls back via a per-statement SAVEPOINT; the good
    statement commits and its awaiter sees ``lastrowid``.

    Reproduces the live trace where a presence FK violation 500ed an
    unrelated, valid ICE-server write in the same batch.
    """
    import asyncio
    import sqlite3 as _sql

    # Use a long batch_timeout_ms so the two enqueues below are
    # guaranteed to be drained into a single batch by the writer
    # loop. Without that the writer can fire on the first statement
    # before the second one arrives.
    db = AsyncDatabase(tmp_dir / "iso.db", batch_timeout_ms=200)
    await db.startup()
    try:
        # Schema with a FK so the bad statement raises ``IntegrityError``.
        await db.enqueue("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        await db.enqueue(
            "CREATE TABLE child(id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES parent(id))"
        )
        await db.enqueue("CREATE TABLE good(val TEXT)")

        async def good_write() -> int:
            return await db.enqueue("INSERT INTO good(val) VALUES(?)", ("ok",))

        async def bad_write() -> int:
            # No parent row with id=999 → FK violation.
            return await db.enqueue("INSERT INTO child(parent_id) VALUES(?)", (999,))

        # Fire both concurrently so they coalesce into one batch.
        good_task = asyncio.create_task(good_write())
        bad_task = asyncio.create_task(bad_write())
        results = await asyncio.gather(good_task, bad_task, return_exceptions=True)
        good_outcome, bad_outcome = results

        # Good write committed; lastrowid is a positive int.
        assert isinstance(good_outcome, int), (
            f"good write should succeed, got {good_outcome!r}"
        )
        assert good_outcome > 0

        # Bad write raised, in isolation.
        assert isinstance(bad_outcome, _sql.IntegrityError), (
            f"bad write should raise IntegrityError, got {bad_outcome!r}"
        )

        # The good row is durably committed even though its
        # batch-sibling rolled back at the savepoint.
        assert await db.fetchval("SELECT COUNT(*) FROM good") == 1
        assert await db.fetchval("SELECT val FROM good") == "ok"
        # And no child row leaked through.
        assert await db.fetchval("SELECT COUNT(*) FROM child") == 0
    finally:
        await db.shutdown()


async def test_executemany(tmp_dir):
    """executemany inserts multiple rows."""
    db = AsyncDatabase(tmp_dir / "em.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue("CREATE TABLE nums(n INT)")
    await db.executemany("INSERT INTO nums VALUES(?)", [(1,), (2,), (3,)])
    count = await db.fetchval("SELECT COUNT(*) FROM nums")
    assert count == 3
    await db.shutdown()


async def test_shutdown_is_idempotent(tmp_dir):
    """Calling shutdown() a second time on an already-stopped DB is safe."""
    db = AsyncDatabase(tmp_dir / "dbl.db", batch_timeout_ms=10)
    await db.startup()
    await db.shutdown()
    await db.shutdown()  # must not raise


async def test_operations_before_startup_raise(tmp_dir):
    """Reads/writes before startup() raise RuntimeError."""
    db = AsyncDatabase(tmp_dir / "not-started.db", batch_timeout_ms=10)
    with pytest.raises(RuntimeError, match="not started"):
        await db.fetchone("SELECT 1")


async def test_concurrent_reads_do_not_misuse_connection(tmp_dir):
    """Many parallel reads on the shared connection must serialise
    via the threading lock. Without it, ``conn.execute()`` races on
    the executor pool and surfaces as
    ``sqlite3.InterfaceError: bad parameter or other API misuse``.
    """
    import asyncio

    db = AsyncDatabase(tmp_dir / "concurrent.db", batch_timeout_ms=10)
    await db.startup()
    try:
        # Burst 50 parallel SELECTs. Pre-fix: surfaces InterfaceError
        # within ~10–20 attempts under load. Post-fix: all succeed.
        results = await asyncio.gather(
            *[db.fetchval("SELECT COUNT(*) FROM users", default=0) for _ in range(50)],
        )
        assert all(r == 0 for r in results)
    finally:
        await db.shutdown()


async def test_foreign_keys_enforced_after_full_migration_chain(tmp_dir):
    """The long-lived writer must leave startup() with FK enforcement ON.

    ``PRAGMA foreign_keys`` is a silent no-op inside a transaction, so a
    migration that disables enforcement to rebuild a table (0046 does)
    can fail to restore it — and ``_open()`` hands that same connection
    to the whole process, making every runtime ``ON DELETE CASCADE``
    inert for the rest of the boot.
    """
    db = AsyncDatabase(tmp_dir / "fk.db", batch_timeout_ms=10)
    await db.startup()
    try:
        assert await db.fetchval("PRAGMA foreign_keys") == 1
    finally:
        await db.shutdown()

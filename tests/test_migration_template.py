"""The suite's migrated-schema template must be indistinguishable from a real
migration run — otherwise every test on a restored DB tests the wrong schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from socialhome.config import Config
from socialhome.db import database as db_module
from socialhome.db.database import AsyncDatabase
from socialhome.db.migrations import discover_migrations, run_migrations

from tests.migration_template import MigrationTemplateCache


def _open(path: Path) -> sqlite3.Connection:
    # Same connection shape as ``AsyncDatabase.startup``.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()


def _versions(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT version, description FROM schema_version ORDER BY version"
    ).fetchall()


def _rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every row of every table except the stamp table's timestamps —
    migrations seed rows (config defaults, ...) and those must match too."""
    out: dict[str, list[tuple]] = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name != 'schema_version'"
    ):
        out[name] = sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
    return out


def test_restored_db_is_identical_to_a_real_migration_run(tmp_path):
    cache = MigrationTemplateCache(run_migrations)
    try:
        real = _open(tmp_path / "real.db")
        applied_real = run_migrations(real)

        first = _open(tmp_path / "first.db")
        applied_first = cache(first)
        second = _open(tmp_path / "second.db")
        applied_second = cache(second)

        assert (cache.misses, cache.hits) == (1, 1)
        assert applied_real == applied_first == applied_second
        assert [m.version for m in applied_second] == [
            m.version for m in discover_migrations()
        ]
        for restored in (first, second):
            assert _schema(restored) == _schema(real)
            assert _versions(restored) == _versions(real)
            assert _rows(restored) == _rows(real)
            assert restored.execute("PRAGMA user_version").fetchone() == (
                real.execute("PRAGMA user_version").fetchone()
            )
            # Still a WAL file with FK enforcement, like AsyncDatabase needs.
            assert restored.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert restored.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        cache.close()


def test_restore_persists_to_disk(tmp_path):
    """A restored DB is a real file other connections (backup, export,
    a restart) can open — not state living only on the one connection."""
    cache = MigrationTemplateCache(run_migrations)
    try:
        cache(_open(tmp_path / "warm.db"))
        conn = _open(tmp_path / "restored.db")
        cache(conn)
        conn.close()
        reopened = sqlite3.connect(tmp_path / "restored.db")
        assert _versions(reopened)
        # A second open runs the REAL runner (non-empty DB) and is a no-op.
        assert run_migrations(reopened) == []
    finally:
        cache.close()


def test_non_empty_db_always_takes_the_real_runner(tmp_path):
    calls: list[str] = []

    def spy(conn, *, directory=None):
        calls.append("real")
        return run_migrations(conn, directory=directory)

    cache = MigrationTemplateCache(spy)
    try:
        cache(_open(tmp_path / "warm.db"))
        seeded = _open(tmp_path / "seeded.db")
        seeded.execute("CREATE TABLE legacy_shape (x INTEGER)")
        cache(seeded)
        assert calls == ["real", "real"]
        assert cache.hits == 0
    finally:
        cache.close()


def test_a_changed_migrations_directory_is_not_served_a_stale_template(tmp_path):
    migdir = tmp_path / "migs"
    migdir.mkdir()
    (migdir / "0001_a.sql").write_text("CREATE TABLE a (x INTEGER);")
    cache = MigrationTemplateCache(run_migrations)
    try:
        cache(_open(tmp_path / "one.db"), directory=migdir)
        (migdir / "0002_b.sql").write_text("CREATE TABLE b (x INTEGER);")
        conn = _open(tmp_path / "two.db")
        applied = cache(conn, directory=migdir)
        assert [m.version for m in applied] == [1, 2]
        assert cache.hits == 0
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"a", "b"} <= names
    finally:
        cache.close()


def test_a_failing_migration_still_raises_and_is_not_cached(tmp_path):
    migdir = tmp_path / "bad"
    migdir.mkdir()
    (migdir / "0001_bad.sql").write_text("THIS IS NOT SQL;")
    cache = MigrationTemplateCache(run_migrations)
    try:
        for name in ("x.db", "y.db"):
            with pytest.raises(sqlite3.OperationalError):
                cache(_open(tmp_path / name), directory=migdir)
        assert (cache.hits, cache.misses) == (0, 0)
    finally:
        cache.close()


def test_the_suite_routes_async_database_through_the_cache(
    _fast_test_databases,
):
    """The conftest wiring is live: ``AsyncDatabase`` resolves the runner
    through its module global, which is the session cache."""
    assert db_module.run_migrations is _fast_test_databases


async def test_async_database_on_a_restored_template_is_usable(
    tmp_path, _fast_test_databases
):
    for name in ("a.db", "b.db"):
        db = AsyncDatabase(tmp_path / name, batch_timeout_ms=10)
        await db.startup()
        try:
            await db.enqueue(
                "INSERT INTO instance_config(key, value) VALUES(?, ?)",
                ("k", name),
            )
            assert await db.fetchval(
                "SELECT value FROM instance_config WHERE key='k'"
            ) == (name)
        finally:
            await db.shutdown()
    assert _fast_test_databases.hits >= 1


def test_the_suite_collapses_only_the_fast_write_window(tmp_path):
    """``conftest._fast_test_databases`` shortens the "fast" 10 ms window and
    the unset default (the GFS's 500 ms), and the Config default, to 1 ms, but keeps a longer window a
    test set on purpose — the batching tests depend on it."""
    fast = AsyncDatabase(tmp_path / "fast.db", batch_timeout_ms=10)
    unset = AsyncDatabase(tmp_path / "unset.db")
    config_default = AsyncDatabase(
        tmp_path / "cfg.db",
        batch_timeout_ms=Config().db_write_batch_timeout_ms,
    )
    kept = AsyncDatabase(tmp_path / "kept.db", batch_timeout_ms=200)
    assert fast._batch_timeout == unset._batch_timeout == 0.001
    assert config_default._batch_timeout == 0.001
    assert kept._batch_timeout == 0.2

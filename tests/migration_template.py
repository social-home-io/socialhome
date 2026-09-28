"""Per-process cache of fully-migrated SQLite schemas for the test suite.

Every ``AsyncDatabase.startup()`` in a test used to replay the whole
migration chain (~57 files, ~0.15 s) against a fresh file — and the suite
boots thousands of databases (``db`` fixture, ``create_app``, the GFS
server, ad-hoc ``AsyncDatabase(...)`` in ~140 test files). This module
wraps the runner at the boundary ``AsyncDatabase`` calls it through
(``socialhome.db.database.run_migrations``):

* The first time a given migrations directory is applied to an *empty*
  database, the **real** runner does the work on the real connection, and
  the result is snapshotted into an in-memory template.
* Every later empty database for the same directory (same files, sizes,
  mtimes) is restored from that snapshot with SQLite's online-backup API —
  a byte-for-byte page copy of what the real runner produced.
* A database that already has any schema object (a test that seeds a
  pre-migration shape, a restart of an existing file) always goes through
  the real runner, so upgrade paths are exercised exactly as before.

Tests of the runner itself import ``run_migrations`` from
``socialhome.db.migrations`` directly and are untouched by this wrapper.
``tests/test_migration_template.py`` pins that a restored database is
identical to a freshly migrated one.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from socialhome.db.migrations import MIGRATIONS_DIR, Migration

RunMigrations = Callable[..., list[Migration]]

_DirKey = tuple[str, tuple[tuple[str, int, int], ...]]


def _dir_key(directory: Path | None) -> _DirKey:
    """Identify a migrations directory by its path AND its file contents'
    fingerprint, so a test that writes extra files into a temp directory can
    never be served a stale snapshot."""
    base = Path(directory) if directory is not None else MIGRATIONS_DIR
    entries: list[tuple[str, int, int]] = []
    if base.exists():
        for path in base.iterdir():
            if path.is_file():
                st = path.stat()
                entries.append((path.name, st.st_size, st.st_mtime_ns))
    return str(base.resolve()), tuple(sorted(entries))


def _is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None


class MigrationTemplateCache:
    """Callable drop-in for ``run_migrations(conn, *, directory=None)``."""

    def __init__(self, real: RunMigrations) -> None:
        self._real = real
        self._lock = threading.Lock()
        self._templates: dict[_DirKey, tuple[sqlite3.Connection, list[Migration]]] = {}
        #: Counters so the self-test can prove which path a call took.
        self.hits = 0
        self.misses = 0

    def __call__(
        self,
        conn: sqlite3.Connection,
        *,
        directory: Path | None = None,
    ) -> list[Migration]:
        if not _is_empty(conn):
            return self._real(conn, directory=directory)
        key = _dir_key(directory)
        with self._lock:
            cached = self._templates.get(key)
            if cached is None:
                applied = self._real(conn, directory=directory)
                template = sqlite3.connect(":memory:", check_same_thread=False)
                conn.backup(template)
                self._templates[key] = (template, list(applied))
                self.misses += 1
                return applied
            template, applied = cached
            template.backup(conn)
            self.hits += 1
            return list(applied)

    def close(self) -> None:
        with self._lock:
            for template, _ in self._templates.values():
                template.close()
            self._templates.clear()

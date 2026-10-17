"""Bring a migration test's database up to "every migration through N".

Each ``tests/db/test_migration_00NN_*.py`` (and the GFS ones) builds the
schema just before the migration under test, seeds rows, then applies that
one migration. Replaying the ~100-file chain on a fresh on-disk database —
one commit per migration — per test was the slowest setup in the suite
(seconds per test on a slow disk).

:func:`apply_through` keeps the per-file helper's exact semantics (apply,
in order, every migration not yet recorded in ``schema_version`` up to
``last``) and adds one shortcut: when the connection holds nothing but an
empty ``schema_version`` table, the result is a SQLite backup-API page copy
of a snapshot built once per process by running the very same loop on an
in-memory database with the same ``schema_version`` DDL, ``foreign_keys``
setting and transaction mode. Anything else (rows already seeded, a
partially migrated database, an open transaction) takes the real loop, so
upgrade paths are exercised exactly as before.

``tests/test_migration_chain.py`` pins that a restored database matches a
replayed one.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from socialhome.db.migrations import MIGRATIONS_DIR, discover_migrations
from tests.migration_template import dir_key

_Key = tuple[object, ...]

_lock = threading.Lock()
_snapshots: dict[_Key, sqlite3.Connection] = {}


def _replay(conn: sqlite3.Connection, last: int, directory: Path | None) -> None:
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    for mig in discover_migrations(directory) if directory else discover_migrations():
        if mig.version in done:
            continue
        if mig.version > last:
            break
        mig.apply(conn)
        conn.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (mig.version, mig.description),
        )


def _fresh_key(
    conn: sqlite3.Connection, last: int, directory: Path | None
) -> _Key | None:
    """Cache key when ``conn`` holds only an empty ``schema_version``."""
    if conn.in_transaction:
        return None
    objects = conn.execute("SELECT name, sql FROM sqlite_master").fetchall()
    if len(objects) != 1 or objects[0][0] != "schema_version":
        return None
    if conn.execute("SELECT 1 FROM schema_version LIMIT 1").fetchone():
        return None
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    return (
        last,
        dir_key(directory or MIGRATIONS_DIR),
        objects[0][1],
        foreign_keys,
        conn.isolation_level,
    )


def apply_through(
    conn: sqlite3.Connection, last: int, *, directory: Path | None = None
) -> None:
    key = _fresh_key(conn, last, directory)
    if key is None:
        _replay(conn, last, directory)
        return
    with _lock:
        snapshot = _snapshots.get(key)
        if snapshot is None:
            snapshot = sqlite3.connect(
                ":memory:",
                isolation_level=conn.isolation_level,
                check_same_thread=False,
            )
            conn.backup(snapshot)  # the same empty schema_version
            snapshot.execute(f"PRAGMA foreign_keys={key[3]}")
            _replay(snapshot, last, directory)
            if snapshot.in_transaction:
                snapshot.commit()
            _snapshots[key] = snapshot
        snapshot.backup(conn)


def clear() -> None:
    """Drop every snapshot (tests of this module; session teardown)."""
    with _lock:
        for snapshot in _snapshots.values():
            snapshot.close()
        _snapshots.clear()

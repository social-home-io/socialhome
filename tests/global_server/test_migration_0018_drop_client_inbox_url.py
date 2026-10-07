"""GFS migration 0018 — drop ``client_instances.inbox_url``.

The connection server used a household's registered inbox URL only for an
HTTPS delivery fallback that could never succeed, so it kept a network
address it had no use for. The column is dropped — a privacy purge: the
stored values must be gone from the file, not merely unused.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from socialhome.db.migrations import discover_migrations

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)
_VERSION = 18


def _apply_through(conn: sqlite3.Connection, last: int) -> None:
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    for mig in discover_migrations(_GFS_MIGRATIONS):
        if mig.version in done:
            continue
        if mig.version > last:
            break
        mig.apply(conn)
        conn.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (mig.version, mig.description),
        )


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "gfs.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    _apply_through(c, _VERSION - 1)
    yield c
    c.close()


def test_migration_is_the_next_version():
    versions = [m.version for m in discover_migrations(_GFS_MIGRATIONS)]
    assert _VERSION in versions
    assert versions == sorted(versions)


def test_a_fresh_database_has_no_inbox_url_column(conn):
    _apply_through(conn, _VERSION)
    assert "inbox_url" not in _columns(conn, "client_instances")
    # Every other column survives — the drop is the whole change.
    assert {
        "instance_id",
        "display_name",
        "public_key",
        "status",
        "auto_accept",
        "connected_at",
        "keywrap_public_key",
        "kem_suite",
        "keywrap_sig",
        "relay_seen_at",
    } <= set(_columns(conn, "client_instances"))


def test_a_registered_address_is_purged_and_the_row_survives(conn):
    """A household registered before 0018 keeps its row — identity, key,
    status, key-wrap material — but the address it registered is gone from
    the file: the column is absent, and the bytes no longer occur in the
    database at all."""
    assert "inbox_url" in _columns(conn, "client_instances")
    conn.execute(
        "INSERT INTO client_instances(instance_id, display_name, public_key,"
        " inbox_url, status, keywrap_public_key, kem_suite)"
        " VALUES('home.example', 'Home', ?, ?, 'active', ?, 'x25519')",
        ("ab" * 32, "https://household-address.example/federation/inbox", "cd" * 32),
    )
    conn.execute(
        "INSERT INTO global_spaces(space_id, owning_instance, name, status)"
        " VALUES('sp-1', 'home.example', 'Space', 'active')"
    )

    _apply_through(conn, _VERSION)
    conn.execute("VACUUM")

    assert "inbox_url" not in _columns(conn, "client_instances")
    assert conn.execute(
        "SELECT instance_id, display_name, public_key, status,"
        " keywrap_public_key, kem_suite FROM client_instances"
    ).fetchall() == [("home.example", "Home", "ab" * 32, "active", "cd" * 32, "x25519")]
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        conn.execute("SELECT inbox_url FROM client_instances")
    # The space still references the surviving row (FK intact).
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute(
        "SELECT owning_instance FROM global_spaces WHERE space_id='sp-1'"
    ).fetchone() == ("home.example",)
    raw = Path(conn.execute("PRAGMA database_list").fetchone()[2]).read_bytes()
    assert b"household-address.example" not in raw


def test_an_old_version_insert_naming_the_column_fails(conn):
    """Code from before 0018 names ``inbox_url`` in its INSERT and in the
    subscriber SELECT. Against a migrated DB (several GFS processes share one
    ``gfs.db``) both fail — upgrade every cluster node together."""
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.OperationalError, match="no column named inbox_url"):
        conn.execute(
            "INSERT INTO client_instances(instance_id, public_key, inbox_url, status)"
            " VALUES('old', ?, 'http://old/wh', 'active')",
            ("ab" * 32,),
        )

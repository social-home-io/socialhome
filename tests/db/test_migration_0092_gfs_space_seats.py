"""Migration 0092 — ``gfs_space_seats``.

Additive: one table keyed by the server's own id (survives a re-pair), and a
backfill from the v_44 mirror provenance where the seating connection still
exists and a local follower seat is held.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 92


def _apply_through(conn: sqlite3.Connection, last: int) -> None:
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    for mig in discover_migrations():
        if mig.version in done:
            continue
        if mig.version > last:
            break
        mig.apply(conn)
        conn.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (mig.version, mig.description),
        )


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    _apply_through(c, _VERSION - 1)
    yield c
    c.close()


def _space(conn, space_id: str, mirror_gfs_id: str | None) -> None:
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, mirror_gfs_id) VALUES(?,'S','remote','x','ab',?)",
        (space_id, mirror_gfs_id),
    )


def _member(conn, space_id: str, user_id: str, role: str, *, local: bool) -> None:
    if local:
        conn.execute(
            "INSERT OR IGNORE INTO users(username, user_id, display_name)"
            " VALUES(?,?,?)",
            (user_id, user_id, user_id),
        )
    conn.execute(
        "INSERT INTO space_members(space_id, user_id, role, joined_at)"
        " VALUES(?,?,?,'2025-01-01')",
        (space_id, user_id, role),
    )


def test_backfills_resolvable_follower_mirrors_only(conn):
    conn.execute(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at)"
        " VALUES('conn-1','gfs-a','G','pk','https://g','active','2025-01-01')"
    )
    _space(conn, "sp-follow", "conn-1")  # seated, local follower → backfilled
    _member(conn, "sp-follow", "u1", "subscriber", local=True)
    _space(conn, "sp-gone", "conn-dead")  # connection re-paired away
    _member(conn, "sp-gone", "u1", "subscriber", local=True)
    _space(conn, "sp-legacy", None)  # pre-v44, no provenance
    _member(conn, "sp-legacy", "u1", "subscriber", local=True)
    _space(conn, "sp-refused", "conn-1")  # mirrored, no subscriber seat
    _space(conn, "sp-remote", "conn-1")  # only a non-local subscriber row
    _member(conn, "sp-remote", "far-user", "subscriber", local=False)
    _apply_through(conn, _VERSION)
    rows = conn.execute(
        "SELECT space_id, gfs_instance_id, gfs_connection_id, gfs_public_key"
        " FROM gfs_space_seats ORDER BY space_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("sp-follow", "gfs-a", "conn-1", "pk")]


def test_one_row_per_space_and_server(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO gfs_space_seats(space_id, gfs_instance_id) VALUES('sp-x','gfs-x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_space_seats(space_id, gfs_instance_id)"
            " VALUES('sp-x','gfs-x')"
        )
    # No FK to spaces / gfs_connections: a seat outlives both.
    row = conn.execute("SELECT seated_at FROM gfs_space_seats").fetchone()
    assert row["seated_at"]

"""Migration 0081 — ``remote_instances.gfs_relay`` + ``peer_gfs_routes``.

Additive: an existing paired row survives untouched with the relay OFF
(``gfs_relay = 0``), and a route row cascades away with either parent —
the peer's ``remote_instances`` row or our own ``gfs_connections`` row.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 81


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


def _insert_peer(conn: sqlite3.Connection, peer_id: str) -> None:
    conn.execute(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id) VALUES(?,?,?,?,?,?,?)",
        (peer_id, "Peer", "aa" * 32, "k1", "k2", "", f"inbox-{peer_id}"),
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
    _insert_peer(c, "peer-1")
    c.execute(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at)"
        " VALUES('gfs-1', 'gi-1', 'GFS', 'pk', 'https://gfs.example', 'active',"
        " '2026-01-01T00:00:00+00:00')"
    )
    yield c
    c.close()


def test_existing_peer_survives_with_the_relay_off(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT id, display_name, gfs_relay FROM remote_instances"
    ).fetchone()
    assert (row["id"], row["display_name"]) == ("peer-1", "Peer")
    assert row["gfs_relay"] == 0


def test_gfs_relay_is_a_boolean(conn):
    _apply_through(conn, _VERSION)
    conn.execute("UPDATE remote_instances SET gfs_relay=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE remote_instances SET gfs_relay=2")


def _add_route(conn: sqlite3.Connection, peer_id: str = "peer-1") -> None:
    conn.execute(
        "INSERT INTO peer_gfs_routes(instance_id, gfs_connection_id,"
        " confirmed_at, last_ack_at) VALUES(?, 'gfs-1', 't0', 't0')",
        (peer_id,),
    )


def test_route_cascades_with_the_peer(conn):
    _apply_through(conn, _VERSION)
    _add_route(conn)
    conn.execute("DELETE FROM remote_instances WHERE id='peer-1'")
    assert conn.execute("SELECT COUNT(*) FROM peer_gfs_routes").fetchone()[0] == 0


def test_route_cascades_with_the_gfs_connection(conn):
    _apply_through(conn, _VERSION)
    _add_route(conn)
    conn.execute("DELETE FROM gfs_connections WHERE id='gfs-1'")
    assert conn.execute("SELECT COUNT(*) FROM peer_gfs_routes").fetchone()[0] == 0


def test_route_needs_a_known_peer_and_connection(conn):
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        _add_route(conn, peer_id="nobody")


def test_cascade_index_exists(conn):
    _apply_through(conn, _VERSION)
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='peer_gfs_routes'"
        )
    }
    assert "idx_peer_gfs_routes_gfs" in names

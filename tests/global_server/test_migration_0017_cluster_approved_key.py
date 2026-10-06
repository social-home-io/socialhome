"""GFS migration 0017 — ``cluster_nodes.approved_key``, the admin trust anchor.

Before operator-approved membership, a first-contact ``NODE_HELLO`` wrote
whatever key it carried into ``public_key`` (TOFU), and older builds DERIVED
each node's key from ``sha256("gfs-cluster-" + instance_id)`` — computable by
anyone. Old-version nodes sharing the DB during a rolling upgrade keep
writing ``public_key``. So membership moves to a new column only admin
add-peer writes; every existing row starts unapproved.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.db.migrations import discover_migrations

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)
_VERSION = 17


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


def test_adds_an_empty_approved_key_and_keeps_every_other_column(conn):
    """No pre-existing row was ever admin-approved (the old add-peer stored no
    key), so every row starts with an empty ``approved_key``; ``public_key``
    and liveness are kept as they were — they are no longer trusted."""
    own = ed25519_public_key(b"\x07" * 32).hex()
    foreign = ed25519_public_key(b"\x09" * 32).hex()
    derived = ed25519_public_key(
        hashlib.sha256(b"gfs-cluster-legacy-node").digest()
    ).hex()
    for node_id, key in (
        ("sibling", own),
        ("tofu", foreign),
        ("legacy-node", derived),
        ("blank", ""),
    ):
        conn.execute(
            "INSERT INTO cluster_nodes(node_id, url, public_key, status, last_seen)"
            " VALUES(?, ?, ?, 'online', '2026-01-01 00:00:00')",
            (node_id, f"http://{node_id}.test", key),
        )
    _apply_through(conn, _VERSION)
    rows = conn.execute(
        "SELECT node_id, public_key, approved_key, status, last_seen"
        " FROM cluster_nodes ORDER BY node_id"
    ).fetchall()
    assert rows == [
        ("blank", "", "", "online", "2026-01-01 00:00:00"),
        ("legacy-node", derived, "", "online", "2026-01-01 00:00:00"),
        ("sibling", own, "", "online", "2026-01-01 00:00:00"),
        ("tofu", foreign, "", "online", "2026-01-01 00:00:00"),
    ]


def test_an_old_version_insert_gets_an_empty_approved_key(conn):
    """Code from before the column never names it: its INSERT (a TOFU row on
    a shared DB during a rolling upgrade) must land unapproved."""
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO cluster_nodes(node_id, url, public_key, status, last_seen)"
        " VALUES('tofu', 'http://tofu.test', ?, 'online', NULL)",
        ("ab" * 32,),
    )
    assert conn.execute(
        "SELECT approved_key FROM cluster_nodes WHERE node_id='tofu'"
    ).fetchone() == ("",)
    col = {
        r[1]: r for r in conn.execute("PRAGMA table_info(cluster_nodes)").fetchall()
    }["approved_key"]
    # (cid, name, type, notnull, dflt_value, pk)
    assert (col[2], col[3], col[4]) == ("TEXT", 1, "''")

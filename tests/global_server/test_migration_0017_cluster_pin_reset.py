"""GFS migration 0017 — every pre-existing ``cluster_nodes`` pin is cleared.

Before operator-approved membership, a first-contact ``NODE_HELLO`` pinned
whatever key it carried (TOFU), and older builds DERIVED each node's key
from ``sha256("gfs-cluster-" + instance_id)`` — computable by anyone. Pins
are immutable now, so such a row would stay a member forever. The upgrade
clears them all once; shared-seed siblings re-pin on their next HELLO and
distinct-key peers are re-added by the operator.
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


def test_every_pre_existing_pin_is_cleared(conn):
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
        "SELECT node_id, url, public_key, status, last_seen FROM cluster_nodes"
        " ORDER BY node_id"
    ).fetchall()
    # Pins gone; everything else (URL, liveness) is kept.
    assert rows == [
        ("blank", "http://blank.test", "", "online", "2026-01-01 00:00:00"),
        ("legacy-node", "http://legacy-node.test", "", "online", "2026-01-01 00:00:00"),
        ("sibling", "http://sibling.test", "", "online", "2026-01-01 00:00:00"),
        ("tofu", "http://tofu.test", "", "online", "2026-01-01 00:00:00"),
    ]


def test_an_empty_roster_is_a_no_op(conn):
    _apply_through(conn, _VERSION)
    assert conn.execute("SELECT COUNT(*) FROM cluster_nodes").fetchone()[0] == 0

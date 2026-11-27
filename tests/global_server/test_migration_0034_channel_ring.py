"""GFS migration 0034 — the channel ring: existing channels start an empty
ring (head 0, no ring id), existing households read no ring, and a ring
goes with its channel."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.migration_chain import apply_through

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)
CH = "0" * 32


def _conn(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "gfs.db", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def test_existing_rows_start_with_an_empty_ring(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 33, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO gfs_channels(channel_id, channel_suite, channel_pk,"
        " registered_at, last_active_at) VALUES(?, 'ed25519', 'pk', 1, 1)",
        (CH,),
    )
    c.execute(
        "INSERT INTO client_instances(instance_id, display_name, public_key)"
        " VALUES('h1', 'H', 'pk')"
    )
    apply_through(c, 34, directory=_GFS_MIGRATIONS)
    assert c.execute(
        "SELECT head_seq, ring_id, ring_bytes FROM gfs_channels"
    ).fetchone() == (0, None, 0)
    assert c.execute("SELECT channel_ring FROM client_instances").fetchone() == (0,)
    c.close()


def test_a_ring_goes_with_its_channel_and_checks_its_class(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 34, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO gfs_channels(channel_id, channel_suite, channel_pk,"
        " registered_at, last_active_at) VALUES(?, 'ed25519', 'pk', 1, 1)",
        (CH,),
    )
    c.execute(
        "INSERT INTO gfs_channel_ring(channel_id, seq, epoch, ring_class,"
        " payload, bytes, created_at) VALUES(?, 1, 0, 'durable', 'x', 1, 1)",
        (CH,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO gfs_channel_ring(channel_id, seq, epoch, ring_class,"
            " payload, bytes, created_at) VALUES(?, 2, 0, 'forever', 'x', 1, 1)",
            (CH,),
        )
    c.execute("DELETE FROM gfs_channels WHERE channel_id=?", (CH,))
    assert c.execute("SELECT COUNT(*) FROM gfs_channel_ring").fetchone() == (0,)
    c.close()

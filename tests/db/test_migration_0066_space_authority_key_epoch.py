"""Migration 0066 — ``spaces.authority_key_epoch`` (federation v_44).

Additive: an existing space row survives untouched and reads epoch 0 (its
creation-time key), new rows default to 0, and NULL is refused.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 66


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


def test_existing_rows_read_epoch_zero(conn):
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1','Space','inst','alice','ab')"
    )
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT identity_public_key, authority_key_epoch FROM spaces WHERE id='sp1'"
    ).fetchone()
    assert row["identity_public_key"] == "ab"
    assert row["authority_key_epoch"] == 0


def test_new_rows_default_to_zero_and_refuse_null(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp2','Space','inst','alice','ab')"
    )
    assert (
        conn.execute(
            "SELECT authority_key_epoch FROM spaces WHERE id='sp2'"
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spaces SET authority_key_epoch=NULL WHERE id='sp2'")


def test_hosted_spaces_holding_a_seed_are_marked_seed_shared(conn):
    """N3: a pre-v44 owner may have shared its seed and turned delegation
    off without retiring it. Every space THIS household hosts that holds its
    seed is backfilled ``authority_seed_shared_epoch = 0`` so the next admin
    revocation rotates; seedless spaces and other households' stubs stay
    NULL."""
    conn.execute(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES('me','x','y',?)",
        ("a" * 64,),
    )
    rows = (
        ("sp-hosted-seed", "me", "wrapped"),
        ("sp-hosted-noseed", "me", None),
        ("sp-stub-seed", "other", "wrapped"),
    )
    for sid, owner, seed in rows:
        conn.execute(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key, identity_private_key) VALUES(?,?,?,?,?,?)",
            (sid, sid, owner, "u", "ab", seed),
        )
    _apply_through(conn, _VERSION)
    got = {
        r["id"]: r["authority_seed_shared_epoch"]
        for r in conn.execute("SELECT id, authority_seed_shared_epoch FROM spaces")
    }
    assert got == {"sp-hosted-seed": 0, "sp-hosted-noseed": None, "sp-stub-seed": None}


def test_new_follower_columns_default_empty(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp9','S','i','u','ab')"
    )
    row = conn.execute(
        "SELECT mirror_gfs_id, gfs_rotation_seq FROM spaces WHERE id='sp9'"
    ).fetchone()
    assert (row["mirror_gfs_id"], row["gfs_rotation_seq"]) == (None, 0)

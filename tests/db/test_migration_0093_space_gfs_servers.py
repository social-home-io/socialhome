"""Migration 0093 — ``spaces.gfs_servers``: the owner's signed GFS list, a
JSON object or NULL."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations
from tests.db.connections import connect

_VERSION = 93


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
    c = connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    _apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES('old','S','host','anna','ab','global')"
    )
    _apply_through(c, _VERSION)
    return c


def test_existing_rows_stay_null(conn) -> None:
    row = conn.execute("SELECT gfs_servers FROM spaces WHERE id='old'").fetchone()
    assert row[0] is None


@pytest.mark.parametrize(
    "value", ["{}", '{"servers":[],"issued_at":1,"gfs_servers_sig":"x"}']
)
def test_json_objects_accepted(conn, value) -> None:
    conn.execute("UPDATE spaces SET gfs_servers=? WHERE id='old'", (value,))
    row = conn.execute("SELECT gfs_servers FROM spaces WHERE id='old'").fetchone()
    assert row[0] == value


@pytest.mark.parametrize("value", ["not json", "[]", '[{"a":1}]', "3", '"x"', ""])
def test_non_objects_rejected(conn, value) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spaces SET gfs_servers=? WHERE id='old'", (value,))


def test_null_accepted_again(conn) -> None:
    conn.execute("UPDATE spaces SET gfs_servers='{}' WHERE id='old'")
    conn.execute("UPDATE spaces SET gfs_servers=NULL WHERE id='old'")
    row = conn.execute("SELECT gfs_servers FROM spaces WHERE id='old'").fetchone()
    assert row[0] is None


def test_invited_by_instance_is_a_null_default_column(conn) -> None:
    row = conn.execute(
        "SELECT invited_by_instance FROM spaces WHERE id='old'"
    ).fetchone()
    assert row[0] is None
    conn.execute("UPDATE spaces SET invited_by_instance='admin-hh' WHERE id='old'")
    row = conn.execute(
        "SELECT invited_by_instance FROM spaces WHERE id='old'"
    ).fetchone()
    assert row[0] == "admin-hh"


def test_invited_by_epoch_and_removed_at_columns(conn) -> None:
    conn.execute(
        "UPDATE spaces SET invited_by_instance='admin-hh', invited_by_epoch=3"
        " WHERE id='old'"
    )
    assert (
        conn.execute("SELECT invited_by_epoch FROM spaces WHERE id='old'").fetchone()[0]
        == 3
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(space_remote_members)")}
    assert "removed_at" in cols


def test_an_epoch_without_an_inviter_is_rejected(conn) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spaces SET invited_by_epoch=0 WHERE id='old'")


def test_clearing_the_inviter_must_clear_its_epoch(conn) -> None:
    conn.execute(
        "UPDATE spaces SET invited_by_instance='admin-hh', invited_by_epoch=0"
        " WHERE id='old'"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spaces SET invited_by_instance=NULL WHERE id='old'")
    conn.execute(
        "UPDATE spaces SET invited_by_instance=NULL, invited_by_epoch=NULL"
        " WHERE id='old'"
    )


def test_an_inviter_with_no_epoch_yet_is_accepted(conn) -> None:
    conn.execute("UPDATE spaces SET invited_by_instance='admin-hh' WHERE id='old'")


def _invitation(conn, snapshot) -> None:
    conn.execute(
        "INSERT INTO space_invitations(id, space_id, invited_user_id, invited_by,"
        " remote_instance_id, remote_user_id, invite_token, expires_at,"
        " join_snapshot) VALUES('i1','sp','u','x','inst','u','t','2030-01-01',?)",
        (snapshot,),
    )


def test_join_snapshot_accepts_null_and_objects(conn) -> None:
    _invitation(conn, None)
    conn.execute("UPDATE space_invitations SET join_snapshot='{\"meta\":{}}'")


@pytest.mark.parametrize("value", ["[]", "x", "3"])
def test_join_snapshot_rejects_non_objects(conn, value) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _invitation(conn, value)

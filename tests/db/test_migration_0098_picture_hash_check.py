"""Migration 0098 — picture hashes are 16 lowercase hex, enforced by the DB.

Before #908 a paired peer could store any string as its ``picture_hash``
(``remote_users`` / ``space_members``), and that string is pasted into
``…/picture?v=<hash>`` URLs. The migration repairs rows written back then
(anything that is not exactly 16 lowercase hex → NULL) and installs
``BEFORE INSERT`` / ``BEFORE UPDATE`` triggers so no writer — today's code,
a future regression, a restored backup — can store one again.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 98
_GOOD = "0123456789abcdef"
_BAD = ("ab&sig=evil", "0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0", "")


def _apply_through(conn: sqlite3.Connection, last: int) -> None:
    apply_through(conn, last)


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
    c.execute(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id) VALUES('peer','Peer','00','k1','k2','https://p/wh','wh')"
    )
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    yield c
    c.close()


def _seed(c: sqlite3.Connection, n: int, value: str | None) -> None:
    c.execute(
        "INSERT INTO users(username, user_id, display_name, picture_hash)"
        " VALUES(?,?,?,?)",
        (f"u{n}", f"uid-{n}", f"U{n}", value),
    )
    c.execute(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name, picture_hash) VALUES(?,?,?,?,?)",
        (f"r{n}", "peer", f"r{n}", f"R{n}", value),
    )
    c.execute(
        "INSERT INTO space_members(space_id, user_id, picture_hash) VALUES(?,?,?)",
        ("sp", f"uid-{n}", value),
    )


_TABLES = (
    ("users", "user_id", "uid-{n}"),
    ("remote_users", "user_id", "r{n}"),
    ("space_members", "user_id", "uid-{n}"),
)


def test_repairs_bad_hashes_and_keeps_good_ones(conn):
    _seed(conn, 0, _GOOD)
    _seed(conn, 1, None)
    for i, bad in enumerate(_BAD, start=2):
        _seed(conn, i, bad)
    _apply_through(conn, _VERSION)
    for table, key, fmt in _TABLES:
        got = {
            r[0]: r[1] for r in conn.execute(f"SELECT {key}, picture_hash FROM {table}")
        }
        assert got[fmt.format(n=0)] == _GOOD, table
        assert got[fmt.format(n=1)] is None, table
        for i in range(2, 2 + len(_BAD)):
            assert got[fmt.format(n=i)] is None, (table, i)


@pytest.mark.parametrize("bad", _BAD)
@pytest.mark.parametrize(("table", "key", "fmt"), _TABLES)
def test_db_refuses_a_bad_hash_on_insert_and_update(conn, table, key, fmt, bad):
    _apply_through(conn, _VERSION)
    _seed(conn, 0, _GOOD)
    with pytest.raises(sqlite3.IntegrityError, match="picture_hash"):
        conn.execute(
            f"UPDATE {table} SET picture_hash=? WHERE {key}=?",
            (bad, fmt.format(n=0)),
        )
    with pytest.raises(sqlite3.IntegrityError, match="picture_hash"):
        _seed(conn, 1, bad)


@pytest.mark.parametrize(("table", "key", "fmt"), _TABLES)
def test_db_accepts_a_good_hash_and_null(conn, table, key, fmt):
    _apply_through(conn, _VERSION)
    _seed(conn, 0, None)
    conn.execute(
        f"UPDATE {table} SET picture_hash=? WHERE {key}=?", (_GOOD, fmt.format(n=0))
    )
    conn.execute(
        f"UPDATE {table} SET picture_hash=NULL WHERE {key}=?", (fmt.format(n=0),)
    )
    # Updates that don't touch the column still work on a repaired row.
    conn.execute(
        f"UPDATE {table} SET picture_hash=picture_hash WHERE {key}=?",
        (fmt.format(n=0),),
    )

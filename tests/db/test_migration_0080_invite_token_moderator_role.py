"""Migration 0080 — an invite link may grant a ``moderator`` seat.

SQLite cannot edit a CHECK in place, so 0080 rebuilds
``space_invite_tokens`` (create → copy → drop → rename, the 0065
procedure). These tests run it against the REAL pre-0080 schema with
rows of every shape and pin that every value survives, that the index
and the cascade come back, that ``moderator`` is now admitted, and that
``owner`` / junk still are not.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

#: The migration under test.
_VERSION = 80


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
    """Every shipped migration below 0080, opened like ``AsyncDatabase``."""
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


def _seed(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1','Space','inst','alice','ab')"
    )
    for token, role, via, gfs in (
        ("t-member", "member", "gfs", None),
        ("t-sub", "subscriber", "internal", None),
        ("t-admin", "admin", "gfs_legacy", "srv"),
    ):
        c.execute(
            "INSERT INTO space_invite_tokens(token, space_id, created_by,"
            " uses_remaining, created_at, expires_at, role, gfs_id, gfs_token,"
            " gfs_url, uses_total, via)"
            " VALUES(?,'sp1','uid-alice',3,'2026-01-02 03:04:05',"
            " '2026-12-01T00:00:00+00:00',?,?,?,?,5,?)",
            (
                token,
                role,
                gfs,
                f"gt-{token}" if gfs else None,
                "https://gfs.example" if gfs else None,
                via,
            ),
        )


def _rows(c: sqlite3.Connection) -> list[dict]:
    return [
        dict(r) for r in c.execute("SELECT * FROM space_invite_tokens ORDER BY token")
    ]


def test_rows_survive_the_rebuild_value_for_value(conn):
    _seed(conn)
    before = _rows(conn)
    _apply_through(conn, _VERSION)
    assert _rows(conn) == before


def test_the_check_admits_moderator(conn):
    _seed(conn)
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO space_invite_tokens(token, space_id, created_by, role)"
        " VALUES('t-mod','sp1','uid-alice','moderator')"
    )
    conn.execute("UPDATE space_invite_tokens SET role='moderator' WHERE token='t-sub'")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("role", "owner"),  # ownership moves only through transfer_ownership
        ("role", "overlord"),
        ("via", "carrier-pigeon"),  # the 0079 CHECK came back too
    ],
)
def test_the_checks_still_reject_junk(conn, column, value):
    _seed(conn)
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO space_invite_tokens(token, space_id, created_by, {column})"
            " VALUES('t-x','sp1','uid-alice',?)",
            (value,),
        )


def test_the_index_and_foreign_key_come_back(conn):
    _apply_through(conn, _VERSION)
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='space_invite_tokens' AND sql IS NOT NULL"
        )
    }
    assert indexes == {"idx_space_invite_tokens_space"}
    fks = list(conn.execute("PRAGMA foreign_key_list(space_invite_tokens)"))
    assert [(f["table"], f["on_delete"]) for f in fks] == [("spaces", "CASCADE")]
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_space_cascade_still_deletes_the_links(conn):
    _seed(conn)
    _apply_through(conn, _VERSION)
    conn.execute("DELETE FROM spaces WHERE id='sp1'")
    assert conn.execute("SELECT COUNT(*) FROM space_invite_tokens").fetchone()[0] == 0


def test_defaults_are_unchanged(conn):
    _seed(conn)
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO space_invite_tokens(token, space_id, created_by)"
        " VALUES('t-d','sp1','uid-alice')"
    )
    row = conn.execute(
        "SELECT role, via, uses_remaining, gfs_id, uses_total, expires_at,"
        " created_at IS NOT NULL AS stamped"
        " FROM space_invite_tokens WHERE token='t-d'"
    ).fetchone()
    assert tuple(row) == ("member", "gfs", 1, None, None, None, 1)

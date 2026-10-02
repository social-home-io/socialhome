"""Migration 0065 — both space role CHECKs admit ``moderator``.

SQLite cannot edit a CHECK in place, so 0065 rebuilds ``space_members``
and ``space_remote_members`` (create → copy → drop → rename, the 0054
procedure). These tests run it against the REAL pre-0065 schema with rows
in both tables and pin that every value survives, that the indexes come
back, that ``moderator`` is now admitted, and that junk still is not.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

#: The migration under test.
_VERSION = 65


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
    """Every shipped migration below 0065, opened like ``AsyncDatabase``."""
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
    for user, role in (
        ("u-owner", "owner"),
        ("u-admin", "admin"),
        ("u-member", "member"),
        ("u-sub", "subscriber"),
    ):
        c.execute(
            "INSERT INTO space_members(space_id, user_id, role, joined_at,"
            " history_visible_from, location_share_enabled,"
            " space_display_name, picture_hash)"
            " VALUES('sp1',?,?,'2026-01-02 03:04:05','2026-01-01',1,?,?)",
            (user, role, f"name-{user}", f"hash-{user}"),
        )
    for user, role in (
        ("r-admin", "admin"),
        ("r-member", "member"),
        ("r-sub", "subscriber"),
    ):
        c.execute(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
            " user_pk, display_name, joined_at, role, member_version, tombstoned)"
            " VALUES('sp1','peer',?,?,?,'2026-02-03 04:05:06',?,7,?)",
            (user, f"pk-{user}", f"dn-{user}", role, 1 if role == "member" else 0),
        )


def _rows(c: sqlite3.Connection, table: str) -> list[dict]:
    return [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY user_id")]


def test_rows_survive_the_rebuild_value_for_value(conn):
    _seed(conn)
    before_local = _rows(conn, "space_members")
    before_remote = _rows(conn, "space_remote_members")
    _apply_through(conn, _VERSION)
    assert _rows(conn, "space_members") == before_local
    assert _rows(conn, "space_remote_members") == before_remote


def test_both_checks_admit_moderator(conn):
    _seed(conn)
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO space_members(space_id, user_id, role)"
        " VALUES('sp1','u-mod','moderator')"
    )
    conn.execute(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES('sp1','peer','r-mod','moderator')"
    )
    conn.execute("UPDATE space_members SET role='moderator' WHERE user_id='u-member'")


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "INSERT INTO space_members(space_id, user_id, role) VALUES('sp1',?,?)",
            ("u-x", "overlord"),
        ),
        (
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES('sp1','peer',?,?)",
            ("r-x", "overlord"),
        ),
        # Ownership stays local-only — a remote seat is never an owner.
        (
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES('sp1','peer',?,?)",
            ("r-y", "owner"),
        ),
    ],
)
def test_both_checks_still_reject_junk(conn, sql, params):
    _seed(conn)
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_indexes_and_foreign_keys_come_back(conn):
    _apply_through(conn, _VERSION)
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN"
            " ('space_members','space_remote_members') AND sql IS NOT NULL"
        )
    }
    assert indexes == {
        "idx_space_members_user",
        "idx_space_remote_members_instance_user",
    }
    for table in ("space_members", "space_remote_members"):
        fks = list(conn.execute(f"PRAGMA foreign_key_list({table})"))
        assert [(f["table"], f["on_delete"]) for f in fks] == [("spaces", "CASCADE")]
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_space_cascade_still_deletes_both_rosters(conn):
    _seed(conn)
    _apply_through(conn, _VERSION)
    conn.execute("DELETE FROM spaces WHERE id='sp1'")
    assert conn.execute("SELECT COUNT(*) FROM space_members").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM space_remote_members").fetchone()[0] == 0


def test_defaults_are_unchanged(conn):
    _seed(conn)
    _apply_through(conn, _VERSION)
    conn.execute("INSERT INTO space_members(space_id, user_id) VALUES('sp1','u-d')")
    conn.execute(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id)"
        " VALUES('sp1','peer','r-d')"
    )
    local = conn.execute(
        "SELECT role, location_share_enabled FROM space_members WHERE user_id='u-d'"
    ).fetchone()
    remote = conn.execute(
        "SELECT role, member_version, tombstoned FROM space_remote_members"
        " WHERE user_id='r-d'"
    ).fetchone()
    assert tuple(local) == ("member", 0)
    assert tuple(remote) == ("member", 0, 0)

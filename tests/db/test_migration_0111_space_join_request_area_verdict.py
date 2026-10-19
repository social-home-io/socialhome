"""Migration 0111 — ``space_join_requests`` keeps a local community's
verdict (``area_verdict``: inside / edge, never a location), and an
applicant household may keep its own request for a space it holds no row
for (the ``space_id`` foreign key is gone; a trigger keeps its cascade)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 111

_INSERT = (
    "INSERT INTO space_join_requests(id, space_id, user_id, expires_at,"
    " area_verdict) VALUES(?, ?, 'u', '2099-01-01', ?)"
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
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'S', 'me', 'anna', ?)",
        ("aa" * 32,),
    )
    c.execute(
        "INSERT INTO space_join_requests(id, space_id, user_id, expires_at,"
        " requested_role) VALUES('old', 'sp', 'u', '2099-01-01', 'admin')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_rows_survive_with_no_verdict(conn):
    row = conn.execute(
        "SELECT requested_role, area_verdict FROM space_join_requests WHERE id='old'"
    ).fetchone()
    assert tuple(row) == ("admin", None)


@pytest.mark.parametrize("verdict", [None, "inside", "edge"])
def test_a_verdict_is_stored(conn, verdict):
    conn.execute(_INSERT, ("r", "sp", verdict))


@pytest.mark.parametrize("verdict", ["outside", "47.38,8.54", ""])
def test_anything_else_is_refused(conn, verdict):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("r", "sp", verdict))


def test_an_applicant_keeps_a_request_for_a_space_it_does_not_hold(conn):
    conn.execute(_INSERT, ("mine", "the-hosts-space", None))


def test_deleting_a_space_still_deletes_its_requests(conn):
    conn.execute(_INSERT, ("r", "sp", "edge"))
    conn.execute("DELETE FROM spaces WHERE id='sp'")
    assert conn.execute("SELECT COUNT(*) FROM space_join_requests").fetchone()[0] == 0


def test_the_indexes_are_back(conn):
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name='space_join_requests'"
            " AND type='index'"
        )
    }
    assert {
        "idx_space_join_requests_pending",
        "idx_space_join_requests_expires",
    } <= names

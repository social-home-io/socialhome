"""Migration 0148 — one pending admin elevation per (space, household,
user id).

A repair (the oldest pending row of a key stays pending, the later ones
become ``expired``) and a partial UNIQUE index, in one step.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 148


def _file(
    c: sqlite3.Connection,
    rid: str,
    *,
    space: str = "sp",
    household: str | None = "peer",
    user: str = "u1",
    role: str | None = "admin",
    status: str = "pending",
    at: str = "2026-11-01 10:00:00",
) -> None:
    c.execute(
        "INSERT INTO space_join_requests(id, space_id, user_id,"
        " remote_applicant_instance_id, requested_role, status, requested_at,"
        " expires_at) VALUES(?,?,?,?,?,?,?,'2099-01-01T00:00:00+00:00')",
        (rid, space, user, household, role, status, at),
    )


def _status(c: sqlite3.Connection) -> dict[str, str]:
    return {
        r["id"]: r["status"]
        for r in c.execute("SELECT id, status FROM space_join_requests")
    }


@pytest.fixture
def before(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    yield c
    c.close()


@pytest.fixture
def conn(before):
    apply_through(before, _VERSION)
    return before


def test_the_repair_keeps_the_oldest_pending_row_of_each_key(before):
    # One household filed three for one person: the oldest stays.
    _file(before, "late", at="2026-11-03 10:00:00")
    _file(before, "first", at="2026-11-01 10:00:00")
    _file(before, "mid", at="2026-11-02 10:00:00")
    # Same moment: the smaller id stays (deterministic).
    _file(before, "tie-b", user="u2")
    _file(before, "tie-a", user="u2")
    # A local member (no household) is a key of its own.
    _file(before, "local-2", household=None, at="2026-11-02 10:00:00")
    _file(before, "local-1", household=None, at="2026-11-01 10:00:00")
    # Not duplicates: another household, another space, an answered one,
    # and ordinary join requests (never touched, however many).
    _file(before, "other-household", household="peer-2")
    _file(before, "other-space", space="sp-2")
    _file(before, "answered", status="denied", at="2026-10-01 10:00:00")
    _file(before, "join-1", role=None)
    _file(before, "join-2", role=None)

    apply_through(before, _VERSION)

    assert _status(before) == {
        "first": "pending",
        "mid": "expired",
        "late": "expired",
        "tie-a": "pending",
        "tie-b": "expired",
        "local-1": "pending",
        "local-2": "expired",
        "other-household": "pending",
        "other-space": "pending",
        "answered": "denied",
        "join-1": "pending",
        "join-2": "pending",
    }
    repaired = before.execute(
        "SELECT reviewed_at, reviewed_by FROM space_join_requests WHERE id='mid'"
    ).fetchone()
    assert repaired["reviewed_at"] and repaired["reviewed_by"] is None


@pytest.mark.parametrize("household", ["peer", None])
def test_a_second_pending_elevation_of_one_key_is_refused(conn, household):
    _file(conn, "r1", household=household)
    with pytest.raises(sqlite3.IntegrityError):
        _file(conn, "r2", household=household)
    conn.execute(
        "INSERT INTO space_join_requests(id, space_id, user_id,"
        " remote_applicant_instance_id, requested_role, expires_at)"
        " VALUES('r3','sp','u1',?,'admin','2099-01-01') ON CONFLICT DO NOTHING",
        (household,),
    )
    assert _status(conn) == {"r1": "pending"}


def test_other_keys_and_answered_rows_are_free(conn):
    _file(conn, "r1")
    _file(conn, "other-household", household="peer-2")
    _file(conn, "local", household=None)
    _file(conn, "other-user", user="u2")
    _file(conn, "other-space", space="sp-2")
    # Ordinary join requests are not covered.
    _file(conn, "join-1", role=None)
    _file(conn, "join-2", role=None)
    # Once answered, the same household may be filed again.
    conn.execute("UPDATE space_join_requests SET status='denied' WHERE id='r1'")
    _file(conn, "r2")
    conn.execute("UPDATE space_join_requests SET status='expired' WHERE id='r2'")
    _file(conn, "r3")
    assert len(_status(conn)) == 9

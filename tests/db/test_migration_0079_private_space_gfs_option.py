"""Migration 0079 — ``spaces.private_gfs`` and ``space_invite_tokens.via``.

A new private space is OFF; the backfill turns the option ON only for the
existing PRIVATE spaces that already have a link-joined household (a
``space_session`` household in ``space_instances``, or a stored v_51 channel —
the member-side mirror). A private space with only live invite links starts
OFF and its links are grandfathered as ``gfs_legacy``; public links keep
``gfs``.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 79


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


def _instance(conn: sqlite3.Connection, iid: str, source: str) -> None:
    conn.execute(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, source) VALUES(?, 'H', 'pk', 'k1', 'k2', '', ?, ?)",
        (iid, f"inbox-{iid}", source),
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
    spaces = {
        "link_member": "private",
        "paired_only": "private",
        "live_link": "private",
        "expired_link": "private",
        "spent_link": "private",
        "public_link": "public",
        "plain": "private",
        "member_stub_channel": "private",
    }
    for sid, kind in spaces.items():
        c.execute(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key, space_type) VALUES(?,'S','host','anna','ab',?)",
            (sid, kind),
        )
    c.execute("UPDATE spaces SET gfs_channel_id='ch1' WHERE id='member_stub_channel'")
    _instance(c, "link-hh", "space_session")
    _instance(c, "paired-hh", "manual")
    c.execute(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('link_member','link-hh')"
    )
    c.execute(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('paired_only','paired-hh')"
    )
    for token, sid, uses, expires in (
        ("t1", "live_link", 1, None),
        ("t2", "expired_link", 1, "2020-01-01T00:00:00+00:00"),
        ("t3", "spent_link", 0, None),
        ("t4", "public_link", 1, None),
    ):
        c.execute(
            "INSERT INTO space_invite_tokens(token, space_id, created_by,"
            " uses_remaining, expires_at) VALUES(?,?,'u',?,?)",
            (token, sid, uses, expires),
        )
    yield c
    c.close()


def _flags(conn) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, private_gfs FROM spaces")}


def test_backfill_turns_on_only_private_spaces_with_a_link_joined_household(conn):
    _apply_through(conn, _VERSION)
    assert _flags(conn) == {
        "link_member": 1,
        "member_stub_channel": 1,
        "live_link": 0,
        "paired_only": 0,
        "expired_link": 0,
        "spent_link": 0,
        "public_link": 0,
        "plain": 0,
    }


def test_links_of_spaces_left_off_are_grandfathered(conn):
    _apply_through(conn, _VERSION)
    vias = dict(conn.execute("SELECT token, via FROM space_invite_tokens"))
    assert vias == {
        "t1": "gfs_legacy",
        "t2": "gfs_legacy",
        "t3": "gfs_legacy",
        "t4": "gfs",
    }


def test_a_new_space_is_off(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES('new','S','host','anna','ab','private')"
    )
    assert _flags(conn)["new"] == 0


def test_checks_refuse_unknown_values(conn):
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spaces SET private_gfs = 2 WHERE id = 'plain'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE space_invite_tokens SET via = 'relay' WHERE token = 't1'")

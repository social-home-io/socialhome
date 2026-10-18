"""Migration 0101 — everyone in the home is a member of its household spaces.

Before this release no code seated anyone in a household-tier space: it held
its creator plus whoever joined by hand, under whatever ``join_mode`` it was
created with. The migration repairs that once for the household spaces THIS
household hosts: ``join_mode`` becomes ``open`` and every active local user
gets a ``member`` seat — never a banned user, a soft-deleted user, a
protected account below the space's ``min_age``, or anyone in a private /
public space or a household-tier stub another household hosts. An existing
seat keeps its role.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 101
_HERE = "home-instance"


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
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (_HERE, "enc", "ab" * 32, "0" * 64),
    )
    yield c
    c.close()


def _user(
    c: sqlite3.Connection,
    uid: str,
    *,
    active: bool = True,
    protected_age: int | None = None,
) -> None:
    c.execute(
        "INSERT INTO users(username, user_id, display_name, state, deleted_at,"
        " child_protection_enabled, declared_age) VALUES(?,?,?,?,?,?,?)",
        (
            f"n-{uid}",
            uid,
            uid,
            "active" if active else "inactive",
            None if active else "2026-10-01T00:00:00+00:00",
            1 if protected_age is not None else 0,
            protected_age,
        ),
    )


def _space(
    c: sqlite3.Connection,
    sid: str,
    *,
    space_type: str = "household",
    join_mode: str = "request",
    host: str = _HERE,
    min_age: int = 0,
    dissolved: int = 0,
) -> None:
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode, min_age, dissolved)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, sid, host, "anna", "ab", space_type, join_mode, min_age, dissolved),
    )


def _seats(c: sqlite3.Connection, sid: str) -> dict[str, str]:
    return {
        r["user_id"]: r["role"]
        for r in c.execute(
            "SELECT user_id, role FROM space_members WHERE space_id=?", (sid,)
        )
    }


def _join_mode(c: sqlite3.Connection, sid: str) -> str:
    return c.execute("SELECT join_mode FROM spaces WHERE id=?", (sid,)).fetchone()[0]


def test_every_active_local_user_is_seated_in_a_hosted_household_space(conn):
    _user(conn, "anna")
    _user(conn, "ben")
    _user(conn, "gone", active=False)
    _space(conn, "home")
    conn.execute(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('home','anna','owner')"
    )
    apply_through(conn, _VERSION)
    # The owner keeps their seat; ben is seated; the soft-deleted user isn't.
    assert _seats(conn, "home") == {"anna": "owner", "ben": "member"}
    assert _join_mode(conn, "home") == "open"


def test_seat_timestamps_are_utc_iso(conn):
    _user(conn, "ben")
    _space(conn, "home")
    apply_through(conn, _VERSION)
    joined = conn.execute(
        "SELECT joined_at FROM space_members WHERE user_id='ben'"
    ).fetchone()[0]
    assert joined.endswith("+00:00") and "T" in joined


def test_banned_users_are_not_seated(conn):
    _user(conn, "ben")
    _space(conn, "home")
    conn.execute(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES('home','ben','anna')"
    )
    apply_through(conn, _VERSION)
    assert _seats(conn, "home") == {}


def test_protected_accounts_follow_the_age_gate(conn):
    _user(conn, "kid12", protected_age=12)
    _user(conn, "teen16", protected_age=16)
    _space(conn, "gated", min_age=13)
    _space(conn, "ungated")
    apply_through(conn, _VERSION)
    assert _seats(conn, "gated") == {"teen16": "member"}
    # No gate: a protected account is a household member like anyone else.
    assert _seats(conn, "ungated") == {"kid12": "member", "teen16": "member"}


def test_other_tiers_and_other_hosts_are_left_alone(conn):
    _user(conn, "ben")
    _space(conn, "priv", space_type="private", join_mode="invite_only")
    _space(conn, "pub", space_type="public", join_mode="request")
    _space(conn, "theirs", host="other-instance")
    _space(conn, "dissolved", dissolved=1)
    apply_through(conn, _VERSION)
    for sid in ("priv", "pub", "theirs", "dissolved"):
        assert _seats(conn, sid) == {}
    assert _join_mode(conn, "priv") == "invite_only"
    assert _join_mode(conn, "pub") == "request"
    # A household-tier stub another household hosts mirrors its host.
    assert _join_mode(conn, "theirs") == "request"


def test_an_existing_seat_keeps_its_role(conn):
    _user(conn, "ben")
    _space(conn, "home")
    conn.execute(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('home','ben','admin')"
    )
    apply_through(conn, _VERSION)
    assert _seats(conn, "home") == {"ben": "admin"}


def test_a_fresh_install_without_identity_is_a_no_op(tmp_path):
    c = sqlite3.connect(tmp_path / "fresh.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION)
    assert c.execute("SELECT COUNT(*) FROM space_members").fetchone()[0] == 0
    c.close()


def test_the_guard_refuses_a_non_open_household_space_hosted_here(conn):
    apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError, match="join_mode open"):
        _space(conn, "new", join_mode="request")
    _space(conn, "ok", join_mode="open")
    with pytest.raises(sqlite3.IntegrityError, match="join_mode open"):
        conn.execute("UPDATE spaces SET join_mode='invite_only' WHERE id='ok'")
    _space(conn, "priv", space_type="private", join_mode="invite_only")
    # Moving a private invite-only space into the household tier without
    # opening it is refused too.
    with pytest.raises(sqlite3.IntegrityError, match="join_mode open"):
        conn.execute("UPDATE spaces SET space_type='household' WHERE id='priv'")
    conn.execute(
        "UPDATE spaces SET space_type='household', join_mode='open' WHERE id='priv'"
    )


def test_the_guard_leaves_other_hosts_and_tiers_alone(conn):
    apply_through(conn, _VERSION)
    # A household-tier stub another household hosts mirrors its host.
    _space(conn, "theirs", host="other-instance", join_mode="request")
    conn.execute("UPDATE spaces SET join_mode='invite_only' WHERE id='theirs'")
    _space(conn, "pub", space_type="public", join_mode="request")
    # Leaving the household tier may set any join mode.
    _space(conn, "home", join_mode="open")
    conn.execute(
        "UPDATE spaces SET space_type='private', join_mode='invite_only'"
        " WHERE id='home'"
    )

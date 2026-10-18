"""Migration 0102 — a follower neither shares nor keeps members' whereabouts.

Owner decision 13: a follower reads a space's posts but is an outsider to its
people. Before #1006 a household whose local users only *follow* a space
(``subscriber`` seats) still received the members' map pins
(``space_remote_member_locations``), the zones (``space_zones``) and the
admin proposal mirror (``space_admin_proposals``); and a follower could turn
their own location sharing on (``space_members.location_share_enabled``).
The migration repairs the rows already on disk, once, and the database then
holds the follower flag at 0 for every writer.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 102
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
    for uid in ("u-fol", "u-mem"):
        c.execute(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (f"n-{uid}", uid, uid),
        )
    yield c
    c.close()


def _space(c: sqlite3.Connection, sid: str, *, host: str) -> None:
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode)"
        " VALUES(?,?,?,?,?,'public','request')",
        (sid, sid, host, "anna", "ab"),
    )


def _seat(c, sid: str, uid: str, role: str, *, sharing: int = 0) -> None:
    c.execute(
        "INSERT INTO space_members(space_id, user_id, role, location_share_enabled)"
        " VALUES(?,?,?,?)",
        (sid, uid, role, sharing),
    )


def _whereabouts(c, sid: str) -> None:
    """A zone, a member's pin and a pending proposal mirror (with a vote)."""
    c.execute(
        "INSERT INTO space_zones(id, space_id, name, latitude, longitude,"
        " radius_m, created_by) VALUES(?,?,?,?,?,?,?)",
        (f"z-{sid}", sid, "School", 47.3769, 8.5417, 150, "u-remote"),
    )
    c.execute(
        "INSERT INTO space_remote_member_locations(space_id, instance_id,"
        " user_id, mode, latitude, longitude) VALUES(?,?,?,'gps',?,?)",
        (sid, "peer", "u-remote", 47.3769, 8.5417),
    )
    c.execute(
        "INSERT INTO space_admin_proposals(id, space_id, action,"
        " proposed_by_instance, proposed_by_user, created_at, expires_at)"
        " VALUES(?,?,'dissolve','peer','u-remote','2026-10-01','2026-11-01')",
        (f"p-{sid}", sid),
    )
    c.execute(
        "INSERT INTO space_admin_proposal_votes(proposal_id, voter_instance,"
        " voter_user, vote, voted_at) VALUES(?,'peer','u-remote','approve','x')",
        (f"p-{sid}",),
    )


def _counts(c, sid: str) -> tuple[int, int, int, int]:
    return tuple(
        c.execute(sql, (sid,)).fetchone()[0]
        for sql in (
            "SELECT COUNT(*) FROM space_zones WHERE space_id=?",
            "SELECT COUNT(*) FROM space_remote_member_locations WHERE space_id=?",
            "SELECT COUNT(*) FROM space_admin_proposals WHERE space_id=?",
            (
                "SELECT COUNT(*) FROM space_admin_proposal_votes v"
                " JOIN space_admin_proposals p ON p.id = v.proposal_id WHERE p.space_id=?"
            ),
        )
    )


def test_a_follower_only_household_drops_the_members_whereabouts(conn):
    _space(conn, "sp-follow", host="peer")
    _seat(conn, "sp-follow", "u-fol", "subscriber")
    _whereabouts(conn, "sp-follow")
    apply_through(conn, _VERSION)
    assert _counts(conn, "sp-follow") == (0, 0, 0, 0)
    orphan_votes = conn.execute(
        "SELECT COUNT(*) FROM space_admin_proposal_votes WHERE proposal_id='p-sp-follow'"
    ).fetchone()[0]
    assert orphan_votes == 0


def test_a_household_with_no_seat_left_drops_them_too(conn):
    _space(conn, "sp-left", host="peer")
    _whereabouts(conn, "sp-left")
    apply_through(conn, _VERSION)
    assert _counts(conn, "sp-left") == (0, 0, 0, 0)


@pytest.mark.parametrize("role", ["owner", "admin", "moderator", "member"])
def test_a_member_household_keeps_them(conn, role):
    _space(conn, "sp-mem", host="peer")
    _seat(conn, "sp-mem", "u-fol", "subscriber")
    _seat(conn, "sp-mem", "u-mem", role)
    _whereabouts(conn, "sp-mem")
    apply_through(conn, _VERSION)
    assert _counts(conn, "sp-mem") == (1, 1, 1, 1)


def test_a_space_hosted_here_keeps_everything(conn):
    """The host is the source of truth for its zones and proposals, whoever
    is seated locally."""
    _space(conn, "sp-host", host=_HERE)
    _seat(conn, "sp-host", "u-fol", "subscriber")
    _whereabouts(conn, "sp-host")
    apply_through(conn, _VERSION)
    assert _counts(conn, "sp-host") == (1, 1, 1, 1)


def test_a_remote_users_seat_row_is_not_a_local_member(conn):
    """Only a local user (``users``) makes this household a member one."""
    _space(conn, "sp-ghost", host="peer")
    _seat(conn, "sp-ghost", "u-fol", "subscriber")
    _seat(conn, "sp-ghost", "u-not-local", "member")
    _whereabouts(conn, "sp-ghost")
    apply_through(conn, _VERSION)
    assert _counts(conn, "sp-ghost") == (0, 0, 0, 0)


def _flag(c, sid: str, uid: str) -> int:
    return c.execute(
        "SELECT location_share_enabled FROM space_members WHERE space_id=? AND user_id=?",
        (sid, uid),
    ).fetchone()[0]


def test_a_followers_location_sharing_is_switched_off(conn):
    _space(conn, "sp-1", host=_HERE)
    _seat(conn, "sp-1", "u-fol", "subscriber", sharing=1)
    _seat(conn, "sp-1", "u-mem", "member", sharing=1)
    apply_through(conn, _VERSION)
    assert _flag(conn, "sp-1", "u-fol") == 0
    assert _flag(conn, "sp-1", "u-mem") == 1


def test_the_database_keeps_a_followers_flag_off(conn):
    """Every writer — the API, the §25.6 roster sync, a restore — lands a
    follower's seat with sharing off; a seat that becomes a follower's
    loses it. A member's flag is untouched."""
    _space(conn, "sp-1", host=_HERE)
    apply_through(conn, _VERSION)
    _seat(conn, "sp-1", "u-fol", "subscriber", sharing=1)
    assert _flag(conn, "sp-1", "u-fol") == 0
    conn.execute(
        "UPDATE space_members SET location_share_enabled=1"
        " WHERE space_id='sp-1' AND user_id='u-fol'"
    )
    assert _flag(conn, "sp-1", "u-fol") == 0
    _seat(conn, "sp-1", "u-mem", "member", sharing=1)
    assert _flag(conn, "sp-1", "u-mem") == 1
    conn.execute(
        "UPDATE space_members SET role='subscriber'"
        " WHERE space_id='sp-1' AND user_id='u-mem'"
    )
    assert _flag(conn, "sp-1", "u-mem") == 0
    conn.execute(
        "INSERT INTO space_members(space_id, user_id, role, location_share_enabled)"
        " VALUES('sp-1','u-fol','subscriber',1)"
        " ON CONFLICT(space_id, user_id) DO UPDATE SET"
        " location_share_enabled=excluded.location_share_enabled"
    )
    assert _flag(conn, "sp-1", "u-fol") == 0

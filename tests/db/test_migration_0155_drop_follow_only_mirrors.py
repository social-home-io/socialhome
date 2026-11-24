"""Migration 0155 — a GFS follow-only mirror (the pre-v_96 ``/gfs/subscribe``
stub) is deleted with its content graph; every space that is anything more
is left alone: a real stub, an owned space, a seed-holding space, a space
with a member seat, and a v_96 follower seat (seated by a redeem ACK, so it
has a household row in ``space_instances``)."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 155

_migration = importlib.import_module(
    "socialhome.migrations.0155_drop_follow_only_mirrors"
)


def _space(
    c: sqlite3.Connection,
    sid: str,
    *,
    owner: str = "host",
    space_type: str = "global",
    mirror: str | None = "gfs-1",
    seed: str | None = None,
) -> None:
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, mirror_gfs_id, identity_private_key)"
        " VALUES(?, ?, ?, 'o', ?, ?, ?, ?)",
        (sid, sid, owner, "ab" * 32, space_type, mirror, seed),
    )


def _seat(c: sqlite3.Connection, sid: str, user: str, role: str) -> None:
    c.execute(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, user, role),
    )


def _spaces(c: sqlite3.Connection) -> set[str]:
    return {r["id"] for r in c.execute("SELECT id FROM spaces")}


@pytest.fixture
def db(tmp_path):
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
        "INSERT INTO instance_identity(id, instance_id, identity_private_key,"
        " identity_public_key, routing_secret)"
        " VALUES('self', 'me', 'sk', 'pk', ?)",
        ("a" * 64,),
    )
    c.execute(
        "INSERT INTO users(username, user_id, display_name)"
        " VALUES('anna', 'u-anna', 'Anna')"
    )
    # A follow-only mirror (provenance) with one local follower and a post.
    _space(c, "mirror")
    _seat(c, "mirror", "u-anna", "subscriber")
    c.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('p1', 'mirror', 'u-host', 'text', 'hi')"
    )
    # A mirror known only by its recorded GFS seat (pre-v_44 provenance).
    _space(c, "seat-only", mirror=None)
    _seat(c, "seat-only", "u-anna", "subscriber")
    c.execute(
        "INSERT INTO gfs_space_seats(space_id, gfs_instance_id)"
        " VALUES('seat-only', 'gfs-x')"
    )
    # A mirror whose last follower already left.
    _space(c, "empty-mirror")
    # A global stub learned from a peer, no GFS evidence.
    _space(c, "peer-stub", mirror=None)
    _seat(c, "peer-stub", "u-anna", "subscriber")
    # Our own global space (listed on a GFS).
    _space(c, "owned", owner="me")
    # A space we hold the authority seed of.
    _space(c, "seeded", seed="wrapped-seed")
    # A mirror row a local user later joined as a member.
    _space(c, "member-seat")
    _seat(c, "member-seat", "u-anna", "member")
    # A v_96 follower seat on a row that was once a mirror: the redeem
    # recorded the host household.
    _space(c, "v96-follow")
    _seat(c, "v96-follow", "u-anna", "subscriber")
    c.execute(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('v96-follow', 'host')"
    )
    # A roster snapshot reached it (remote member rows).
    _space(c, "rostered")
    _seat(c, "rostered", "u-anna", "subscriber")
    c.execute(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id)"
        " VALUES('rostered', 'host', 'u-host')"
    )
    # A space with an invitation (a real invitation stub).
    _space(c, "invited")
    c.execute(
        "INSERT INTO space_invitations(id, space_id, invited_user_id,"
        " invited_by, expires_at) VALUES('i1', 'invited', 'u-anna', 'u-host',"
        " '2099-01-01')"
    )
    # A public (not global) space with mirror-looking provenance.
    _space(c, "public", space_type="public")
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_provable_follow_only_mirrors_are_deleted_with_their_content(db):
    remaining = _spaces(db)
    assert {"mirror", "seat-only", "empty-mirror"}.isdisjoint(remaining)
    assert db.execute("SELECT COUNT(*) FROM space_posts").fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT COUNT(*) FROM space_members WHERE space_id='mirror'"
        ).fetchone()[0]
        == 0
    )


def test_every_space_that_is_more_than_a_mirror_is_kept(db):
    assert _spaces(db) == {
        "peer-stub",
        "owned",
        "seeded",
        "member-seat",
        "v96-follow",
        "rostered",
        "invited",
        "public",
    }


def test_a_database_without_our_identity_deletes_nothing(tmp_path):
    c = sqlite3.connect(tmp_path / "fresh.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    _space(c, "mirror")
    _migration.migrate(c)
    assert _spaces(c) == {"mirror"}
    c.close()


def test_running_twice_is_a_no_op(db):
    before = _spaces(db)
    _migration.migrate(db)
    assert _spaces(db) == before

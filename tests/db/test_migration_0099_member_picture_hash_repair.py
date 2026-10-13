"""Migration 0099 — seats point at the per-space picture blob they hold.

Before #910 every §25.6 ``members`` upsert (and a host's own re-seat) wrote
NULL over ``space_members.picture_hash``; 0098 NULLed hashes that were not
16 lowercase hex. In both cases the blob in ``space_member_profile_pictures``
survived, so the seat said "no picture" while the household held one — and
from v_58 on a provider's ``null`` is a reset its co-members act on. The
migration re-points such a seat at its blob's hash, copying only a hash
0098's triggers accept (anything else would abort the migration and
boot-loop the install).
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 99
_GOOD = "0123456789abcdef"
_OTHER = "fedcba9876543210"
_BAD_BLOB_HASHES = ("ab&sig=evil", "0123456789ABCDEF", "0123456789abcde", "")


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
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    yield c
    c.close()


def _seat(
    c: sqlite3.Connection, uid: str, seat_hash: str | None, *, local: bool = True
) -> None:
    """Seat ``uid``; a LOCAL member is one of this household's ``users``."""
    if local:
        c.execute(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (f"n-{uid}", uid, uid),
        )
    c.execute(
        "INSERT INTO space_members(space_id, user_id, picture_hash) VALUES(?,?,?)",
        ("sp", uid, seat_hash),
    )


def _blob(c: sqlite3.Connection, uid: str, blob_hash: object) -> None:
    c.execute(
        "INSERT INTO space_member_profile_pictures(space_id, user_id,"
        " bytes_webp, hash, width, height) VALUES(?,?,?,?,?,?)",
        ("sp", uid, b"webp", blob_hash, 1, 1),
    )


def _hashes(c: sqlite3.Connection) -> dict[str, str | None]:
    return {
        r[0]: r[1] for r in c.execute("SELECT user_id, picture_hash FROM space_members")
    }


def test_a_remote_members_null_seat_is_left_to_sync(conn):
    """Before this release the live ``SPACE_MEMBER_PROFILE_UPDATED`` reset
    NULLed a REMOTE member's seat and kept the blob — the same shape as the
    wipe damage. Re-pointing it would bring back a picture the member
    removed (and a host would ship it to every joiner), so remote seats are
    left alone: their own household's catch-up settles them."""
    _seat(conn, "remote", None, local=False)
    _blob(conn, "remote", _GOOD)
    _apply_through(conn, _VERSION)
    assert _hashes(conn)["remote"] is None


def test_a_wiped_seat_points_at_its_blob_again(conn):
    _seat(conn, "wiped", None)
    _blob(conn, "wiped", _GOOD)
    _apply_through(conn, _VERSION)
    assert _hashes(conn)["wiped"] == _GOOD


def test_only_null_seats_with_a_valid_blob_hash_change(conn):
    _seat(conn, "no-blob", None)
    _seat(conn, "has-hash", _OTHER)
    _blob(conn, "has-hash", _GOOD)
    for i, bad in enumerate(_BAD_BLOB_HASHES):
        _seat(conn, f"bad-{i}", None)
        _blob(conn, f"bad-{i}", bad)
    # A non-text hash (a BLOB) is not copied either.
    _seat(conn, "blob-typed", None)
    _blob(conn, "blob-typed", _GOOD.encode())
    # The migration never aborts on a blob hash 0098's trigger refuses.
    _apply_through(conn, _VERSION)
    got = _hashes(conn)
    assert got["no-blob"] is None
    assert got["has-hash"] == _OTHER
    for i in range(len(_BAD_BLOB_HASHES)):
        assert got[f"bad-{i}"] is None
    assert got["blob-typed"] is None


def test_a_blob_in_another_space_is_not_taken(conn):
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp2','S2','host','anna','ab')"
    )
    _seat(conn, "u", None)
    conn.execute(
        "INSERT INTO space_member_profile_pictures(space_id, user_id,"
        " bytes_webp, hash, width, height) VALUES('sp2','u',x'00',?,1,1)",
        (_GOOD,),
    )
    _apply_through(conn, _VERSION)
    assert _hashes(conn)["u"] is None

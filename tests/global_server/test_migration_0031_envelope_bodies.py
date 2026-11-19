"""GFS migration 0031 — ``gfs_envelope_bodies``: a multi-recipient frame's
body is stored once; queue rows reference it (``body_id``) and hold their
own wrap (``sealed_json``). The body is freed by a trigger when its last
reference goes, and the mailbox pool total counts it once plus, per
reference, ``REF_ROW_BYTES`` and its wrap."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from socialhome.global_server.repositories import REF_ROW_BYTES
from tests.migration_chain import apply_through

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)


def _conn(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "gfs.db", isolation_level=None)
    # Like production (``db/database.py``): the body_id reference is enforced.
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def _total(c, pool: str = "mailbox") -> int:
    return c.execute(
        "SELECT bytes FROM gfs_queue_totals WHERE queue='envelope' AND pool=?",
        (pool,),
    ).fetchone()[0]


def _body(c, size: int) -> int:
    cur = c.execute(
        "INSERT INTO gfs_envelope_bodies(sealed_json, size_bytes, created_at,"
        " expires_at) VALUES(?, ?, 1, 100)",
        ("x" * size, size),
    )
    return int(cur.lastrowid)


WRAP = '{"kem_suite":"k","eph_pk":"e","ciphertext":"c"}'
W = len(WRAP)


def _ref(c, to: str, body_id: int, size: int, wrap: str = WRAP) -> int:
    cur = c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at, frame_type, size_bytes, pool, body_id)"
        " VALUES(?, ?, 1, 100, 'envelope', ?, 'mailbox', ?)",
        (to, wrap, size + len(wrap), body_id),
    )
    return int(cur.lastrowid)


def test_existing_rows_are_kept_and_the_new_shape_is_additive(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 30, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at, size_bytes, pool) VALUES('m', '{}', 1, 2, 2, 'mailbox')"
    )
    before = _total(c)
    apply_through(c, 31, directory=_GFS_MIGRATIONS)
    assert c.execute("SELECT body_id FROM gfs_envelope_queue").fetchall() == [(None,)]
    assert _total(c) == before == 2
    cols = [r[1] for r in c.execute("PRAGMA table_info(gfs_envelope_bodies)")]
    assert cols == ["id", "sealed_json", "size_bytes", "created_at", "expires_at"]
    c.close()


def test_body_counts_once_and_is_freed_with_its_last_reference(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 31, directory=_GFS_MIGRATIONS)
    body = _body(c, 1000)
    assert _total(c) == 1000
    refs = [_ref(c, to, body, 1000) for to in ("a" * 32, "b" * 32, "c" * 32)]
    # The body is counted once; each reference row adds its fixed row cost
    # and its own wrap.
    assert REF_ROW_BYTES == 64
    assert _total(c) == 1000 + 3 * (REF_ROW_BYTES + W)
    c.execute("DELETE FROM gfs_envelope_queue WHERE id=?", (refs[0],))
    c.execute("DELETE FROM gfs_envelope_queue WHERE id=?", (refs[1],))
    assert c.execute("SELECT COUNT(*) FROM gfs_envelope_bodies").fetchone()[0] == 1
    assert _total(c) == 1000 + REF_ROW_BYTES + W
    c.execute("DELETE FROM gfs_envelope_queue WHERE id=?", (refs[2],))
    assert c.execute("SELECT COUNT(*) FROM gfs_envelope_bodies").fetchone()[0] == 0
    assert _total(c) == 0
    c.close()


def test_plain_rows_still_count_and_bulk_deletes_free_shared_bodies(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 31, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at, size_bytes, pool) VALUES('m', '{\"a\":1}', 1, 2, 7, 'mailbox')"
    )
    assert _total(c) == 7
    body = _body(c, 50)
    _ref(c, "a" * 32, body, 50)
    _ref(c, "b" * 32, body, 50, wrap=WRAP + " ")
    assert _total(c) == 57 + 2 * (REF_ROW_BYTES + W) + 1
    # One statement deleting every reference (the TTL sweep, an idle sweep).
    c.execute("DELETE FROM gfs_envelope_queue")
    assert c.execute("SELECT COUNT(*) FROM gfs_envelope_bodies").fetchone()[0] == 0
    assert _total(c) == 0
    assert _total(c, "household") == 0
    c.close()


def test_a_body_cannot_be_deleted_while_referenced(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 31, directory=_GFS_MIGRATIONS)
    body = _body(c, 10)
    _ref(c, "a" * 32, body, 10)
    try:
        c.execute("DELETE FROM gfs_envelope_bodies WHERE id=?", (body,))
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("foreign key not enforced")
    c.close()

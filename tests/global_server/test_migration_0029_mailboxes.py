"""GFS migration 0029 — ``gfs_mailboxes``: a new table holding only the
pinned mailbox key and two timestamps; the queues are left untouched."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.migration_chain import apply_through

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)


def _conn(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "gfs.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def test_mailboxes_table_holds_no_identity_and_keeps_the_queues(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 28, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at) VALUES('x', '{}', 1, 2)"
    )
    apply_through(c, 29, directory=_GFS_MIGRATIONS)
    assert c.execute("SELECT COUNT(*) FROM gfs_envelope_queue").fetchone()[0] == 1
    cols = [r[1] for r in c.execute("PRAGMA table_info(gfs_mailboxes)")]
    assert cols == ["mailbox_id", "sign_pk", "created_at", "last_claim_at"]
    c.execute(
        "INSERT INTO gfs_mailboxes(mailbox_id, sign_pk, created_at) VALUES('m', ?, 5)",
        (b"k" * 32,),
    )
    assert c.execute(
        "SELECT last_claim_at FROM gfs_mailboxes WHERE mailbox_id='m'"
    ).fetchone() == (None,)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO gfs_mailboxes(mailbox_id, sign_pk, created_at)"
            " VALUES('n', ?, 5)",
            (b"short",),
        )
    c.close()


def test_queue_totals_are_backfilled_and_kept_by_triggers(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 28, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at) VALUES('x', '{\"a\":1}', 1, 2)"  # pre-0014 shape: size 0
    )
    c.execute(
        "INSERT INTO gfs_media_queue(to_instance, body, size_bytes, created_at,"
        " expires_at) VALUES('x', ?, 3, 1, 2)",
        (b"abc",),
    )
    apply_through(c, 29, directory=_GFS_MIGRATIONS)

    def total(queue, pool):
        return c.execute(
            "SELECT bytes FROM gfs_queue_totals WHERE queue=? AND pool=?",
            (queue, pool),
        ).fetchone()[0]

    assert total("envelope", "household") == len('{"a":1}')
    assert total("media", "household") == 3
    c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at, size_bytes, pool) VALUES('m', '{}', 1, 2, 2, 'mailbox')"
    )
    assert total("envelope", "mailbox") == 2
    c.execute("DELETE FROM gfs_envelope_queue")
    assert total("envelope", "household") == 0
    assert total("envelope", "mailbox") == 0
    # Relay rows are not counted (their own caps).
    c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at, frame_type, size_bytes) VALUES('x', '{}', 1, 2, 'relay', 2)"
    )
    assert total("envelope", "household") == 0
    c.execute("INSERT INTO gfs_known_gfs_ids VALUES('gfs-0', 0)")
    c.close()

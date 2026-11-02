"""Migration 0140 — ``media_transcode_jobs.duration_s``: the length a queued
video was checked against at upload. Additive: a waiting job from before it
keeps its row and gets NULL (the protocol's 60 s, as before)."""

from __future__ import annotations

import sqlite3

from tests.db.connections import connect
from tests.migration_chain import apply_through

_VERSION = 140


def _fresh(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def test_a_waiting_job_survives_with_no_length(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO media_transcode_jobs(output_filename, source_path,"
        " thumbnail_filename, owner_user_id) VALUES('a.webm', '/s/a.bin',"
        " 'a.webp', 'u-a')"
    )
    before = [tuple(r) for r in c.execute("SELECT * FROM media_transcode_jobs")]

    apply_through(c, _VERSION)

    rows = c.execute("SELECT * FROM media_transcode_jobs").fetchall()
    assert [tuple(r)[:-1] for r in rows] == before
    assert rows[0]["duration_s"] is None


def test_a_new_job_stores_its_length(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    c.execute(
        "INSERT INTO media_transcode_jobs(output_filename, source_path,"
        " thumbnail_filename, duration_s) VALUES('b.webm', '/s/b.bin', 'b.webp',"
        " 299.97)"
    )
    row = c.execute("SELECT duration_s FROM media_transcode_jobs").fetchone()
    assert row["duration_s"] == 299.97

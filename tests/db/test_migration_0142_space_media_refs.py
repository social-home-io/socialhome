"""Migration 0142 — ``space_media_refs``: where a space file a household
holds no copy of lives in a GFS media cache. A new table; nothing existing is
touched, and the rows go with their space."""

from __future__ import annotations

import sqlite3

import pytest

from tests.db.connections import connect
from tests.migration_chain import apply_through

_VERSION = 142


def _fresh(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _space(c: sqlite3.Connection, space_id: str = "sp-1") -> None:
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'S', 'inst', 'admin', 'aa')",
        (space_id,),
    )


def test_table_shape(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(space_media_refs)")}
    assert cols == {
        "local_name",
        "filename",
        "space_id",
        "blob_id",
        "suite",
        "epoch",
        "size",
        "scope",
        "addr_suite",
        "item_id",
        "origin",
        "created_at",
    }


def test_row_round_trip_and_cascade(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    _space(c)
    c.execute(
        "INSERT INTO space_media_refs(local_name, filename, space_id, blob_id, suite, epoch,"
        " size, scope, addr_suite, item_id) VALUES('sg-a.webp', 'a.webp', 'sp-1', ?,"
        " 'aesgcm-256-pad-det', 3, 65564, 'space', 'sm1', 'post-1')",
        ("a" * 64,),
    )
    row = c.execute("SELECT * FROM space_media_refs").fetchone()
    assert (row["local_name"], row["filename"]) == ("sg-a.webp", "a.webp")
    assert row["origin"] == ""
    assert row["created_at"]
    c.execute("DELETE FROM spaces WHERE id = 'sp-1'")
    assert c.execute("SELECT COUNT(*) FROM space_media_refs").fetchone()[0] == 0


def test_the_local_name_is_the_key(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    _space(c)
    ins = (
        "INSERT INTO space_media_refs(local_name, filename, space_id, blob_id, suite, epoch,"
        " size, scope, addr_suite, item_id) VALUES('sg-a.webp', 'a.webp', 'sp-1', ?, 's', 0, 1,"
        " 'space', 'sm1', 'post-1')"
    )
    c.execute(ins, ("a" * 64,))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins, ("b" * 64,))


def test_size_and_epoch_checks(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    _space(c)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO space_media_refs(local_name, filename, space_id, blob_id, suite,"
            " epoch, size, scope, addr_suite, item_id) VALUES('sg-a.webp', 'a.webp', 'sp-1', ?, 's',"
            " -1, 1, 'space', 'sm1', 'post-1')",
            ("a" * 64,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO space_media_refs(local_name, filename, space_id, blob_id, suite,"
            " epoch, size, scope, addr_suite, item_id) VALUES('sg-a.webp', 'a.webp', 'sp-1', ?, 's',"
            " 0, 0, 'space', 'sm1', 'post-1')",
            ("a" * 64,),
        )


def test_the_scope_is_space_or_channel(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    _space(c)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO space_media_refs(local_name, filename, space_id, blob_id, suite,"
            " epoch, size, scope, addr_suite, item_id) VALUES('sg-a.webp', 'a.webp', 'sp-1', ?, 's',"
            " 0, 1, 'galaxy', 'sm1', 'post-1')",
            ("a" * 64,),
        )


def test_rows_are_looked_up_by_item(tmp_path):
    """The fetcher maps an item's files through its own rows (an edit that
    carries no descriptor keeps the names the item already has)."""
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    plan = " ".join(
        str(r[3])
        for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM space_media_refs"
            " WHERE space_id = 'sp-1' AND item_id = 'post-1'"
        )
    )
    assert "idx_space_media_refs_item" in plan

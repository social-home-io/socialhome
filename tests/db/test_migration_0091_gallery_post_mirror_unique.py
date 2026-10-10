"""Migration 0091 — one system-album item per (source post, file): the
duplicates two concurrent mirrors of the same post left are removed (the
oldest kept, the album recounted) and a unique index keeps them out."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 91


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


def _fresh(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def _album(c: sqlite3.Connection, aid: str, *, system: bool, count: int) -> None:
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'sp', 'h', 'anna', 'ab')"
        " ON CONFLICT DO NOTHING"
    )
    c.execute(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name, is_system,"
        " item_count) VALUES(?, 'sp', ?, ?, ?, ?)",
        (aid, None if system else "u", aid, int(system), count),
    )


def _item(
    c: sqlite3.Connection,
    iid: str,
    album: str,
    filename: str,
    source: str | None,
    created_at: str,
) -> None:
    c.execute(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height, source_post_id, created_at)"
        " VALUES(?, ?, 'u', 'photo', ?, ?, 0, 0, ?, ?)",
        (iid, album, filename, filename, source, created_at),
    )


def _items(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT id FROM gallery_items")}


def test_duplicate_mirrors_go_the_oldest_stays_and_the_album_recounts(tmp_path):
    c = _fresh(tmp_path)
    _apply_through(c, _VERSION - 1)
    _album(c, "sys", system=True, count=5)
    _album(c, "user", system=False, count=2)
    _item(c, "m-old", "sys", "a.webp", "p-1", "2026-01-01")
    _item(c, "m-dup", "sys", "a.webp", "p-1", "2026-01-02")  # the race's twin
    _item(c, "m-b", "sys", "b.webp", "p-1", "2026-01-01")  # another file, kept
    _item(c, "m-dup2", "sys", "b.webp", "p-1", "2026-01-03")
    _item(c, "m-other", "sys", "a.webp", "p-2", "2026-01-01")  # another post
    # Uploads (no source post) may share a file name: untouched.
    _item(c, "u-1", "user", "same.webp", None, "2026-01-01")
    _item(c, "u-2", "user", "same.webp", None, "2026-01-02")

    _apply_through(c, _VERSION)

    assert _items(c) == {"m-old", "m-b", "m-other", "u-1", "u-2"}
    counts = dict(c.execute("SELECT id, item_count FROM gallery_albums").fetchall())
    assert counts == {"sys": 3, "user": 2}


def test_a_second_mirror_of_the_same_file_is_refused(tmp_path):
    c = _fresh(tmp_path)
    _apply_through(c, _VERSION)
    _album(c, "sys", system=True, count=0)
    _item(c, "m-1", "sys", "a.webp", "p-1", "2026-01-01")
    with pytest.raises(sqlite3.IntegrityError):
        _item(c, "m-2", "sys", "a.webp", "p-1", "2026-01-02")
    # The repo's insert is ``ON CONFLICT DO NOTHING``: a racing mirror is a
    # no-op there, not an error.
    c.execute(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height, source_post_id, created_at)"
        " VALUES('m-3', 'sys', 'u', 'photo', 'a.webp', 'a.webp', 0, 0, 'p-1', 'x')"
        " ON CONFLICT DO NOTHING"
    )
    assert _items(c) == {"m-1"}
    # A deleted mirror row (tombstoned in place) does not block a new one.
    c.execute("UPDATE gallery_items SET deleted_at='now' WHERE id='m-1'")
    _item(c, "m-4", "sys", "a.webp", "p-1", "2026-01-03")
    assert _items(c) == {"m-1", "m-4"}

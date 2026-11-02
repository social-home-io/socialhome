"""Migration 0139 — the household gallery's library model: trash (the 0085
columns, reused), ``content_sha256``, album audiences, favourites and the
in-album date index. Additive: every existing row survives unchanged."""

from __future__ import annotations

import sqlite3

import pytest

from tests.db.connections import connect
from tests.migration_chain import apply_through

_VERSION = 139
_SHA = "ab" * 32


def _fresh(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def _seed(c: sqlite3.Connection) -> None:
    """A household with a photo, a trashed-elsewhere space tombstone, a
    remote uploader and the system album — the shapes 0139 must keep."""
    c.execute(
        "INSERT INTO users(username, user_id, display_name) VALUES('anna','u-a','A')"
    )
    c.execute(
        "INSERT INTO users(username, user_id, display_name) VALUES('ben','u-b','B')"
    )
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'sp', 'h', 'anna', 'ab')"
    )
    c.execute(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name, item_count)"
        " VALUES('home', NULL, 'u-a', 'Summer', 2)"
    )
    c.execute(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name, is_system)"
        " VALUES('posts', NULL, NULL, 'Posts', 1)"
    )
    c.execute(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name)"
        " VALUES('sp-album', 'sp', 'u-remote', 'Trip')"
    )
    for iid, album, by, fn, taken, deleted in (
        ("i-1", "home", "u-a", "a.webp", "2026-07-01", None),
        ("i-2", "home", "u-b", "b.webp", None, None),
        ("i-3", "sp-album", "u-remote", "", None, "2026-08-01 10:00:00"),
        ("i-4", "posts", "u-a", "p.webp", None, None),
    ):
        c.execute(
            "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type,"
            " filename, thumbnail_filename, width, height, taken_at,"
            " created_at, deleted_at) VALUES(?, ?, ?, 'photo', ?, ?, 10, 10, ?,"
            " '2026-07-02T08:00:00+00:00', ?)",
            (iid, album, by, fn, fn, taken, deleted),
        )


def _migrated(tmp_path) -> sqlite3.Connection:
    c = _fresh(tmp_path)
    apply_through(c, _VERSION - 1)
    _seed(c)
    apply_through(c, _VERSION)
    return c


def test_every_existing_row_survives_with_the_new_defaults(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION - 1)
    _seed(c)
    before_items = [tuple(r) for r in c.execute("SELECT * FROM gallery_items")]
    before_albums = [tuple(r) for r in c.execute("SELECT * FROM gallery_albums")]

    apply_through(c, _VERSION)

    # The old columns are unchanged and come first: the new ones append.
    after_items = [tuple(r) for r in c.execute("SELECT * FROM gallery_items")]
    after_albums = [tuple(r) for r in c.execute("SELECT * FROM gallery_albums")]
    assert [r[:-1] for r in after_items] == before_items
    assert [r[:-1] for r in after_albums] == before_albums
    assert {r["content_sha256"] for r in c.execute("SELECT * FROM gallery_items")} == {
        None
    }
    assert {r["audience"] for r in c.execute("SELECT * FROM gallery_albums")} == {
        "everyone"
    }
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_content_sha256_takes_only_64_lowercase_hex(tmp_path):
    c = _migrated(tmp_path)
    c.execute("UPDATE gallery_items SET content_sha256=? WHERE id='i-1'", (_SHA,))
    for bad in ("AB" * 32, "ab" * 31, "zz" * 32, ""):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "UPDATE gallery_items SET content_sha256=? WHERE id='i-2'", (bad,)
            )


def test_only_a_household_album_can_be_private(tmp_path):
    c = _migrated(tmp_path)
    c.execute("UPDATE gallery_albums SET audience='chosen' WHERE id='home'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE gallery_albums SET audience='chosen' WHERE id='sp-album'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE gallery_albums SET audience='friends' WHERE id='home'")


def test_audience_and_favourites_follow_their_rows(tmp_path):
    c = _migrated(tmp_path)
    c.execute(
        "INSERT INTO gallery_album_audience(album_id, user_id) VALUES('home','u-b')"
    )
    c.execute("INSERT INTO gallery_favourites(user_id, item_id) VALUES('u-b','i-1')")
    with pytest.raises(sqlite3.IntegrityError):  # a person not in this household
        c.execute(
            "INSERT INTO gallery_album_audience(album_id, user_id)"
            " VALUES('home','u-remote')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # one favourite per person + item
        c.execute(
            "INSERT INTO gallery_favourites(user_id, item_id) VALUES('u-b','i-1')"
        )

    # The trash purge removes the item: its favourites go with it.
    c.execute("DELETE FROM gallery_items WHERE id='i-1'")
    assert c.execute("SELECT COUNT(*) FROM gallery_favourites").fetchone()[0] == 0
    c.execute("DELETE FROM gallery_items WHERE album_id='home'")
    c.execute("DELETE FROM gallery_albums WHERE id='home'")
    assert c.execute("SELECT COUNT(*) FROM gallery_album_audience").fetchone()[0] == 0


def test_removing_a_person_never_removes_their_photos(tmp_path):
    """Leaving the household keeps photos: no FK from the gallery to
    ``users`` cascades (0046), while the person's audience seats and
    favourites — their own private state — go."""
    c = _migrated(tmp_path)
    c.execute(
        "INSERT INTO gallery_album_audience(album_id, user_id) VALUES('home','u-b')"
    )
    c.execute("INSERT INTO gallery_favourites(user_id, item_id) VALUES('u-b','i-1')")

    c.execute("DELETE FROM users WHERE user_id IN ('u-a', 'u-b')")

    assert {r[0] for r in c.execute("SELECT id FROM gallery_items")} == {
        "i-1",
        "i-2",
        "i-3",
        "i-4",
    }
    assert c.execute(
        "SELECT owner_user_id FROM gallery_albums WHERE id='home'"
    ).fetchone()[0] == ("u-a")
    assert c.execute("SELECT COUNT(*) FROM gallery_album_audience").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM gallery_favourites").fetchone()[0] == 0


def _plan(c: sqlite3.Connection, sql: str, params: tuple) -> str:
    return " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_the_new_indexes_serve_their_reads(tmp_path):
    c = _migrated(tmp_path)
    assert "idx_gallery_items_album_taken" in _plan(
        c,
        "SELECT id FROM gallery_items WHERE album_id=? AND deleted_at IS NULL"
        " ORDER BY COALESCE(taken_at, substr(created_at, 1, 10)) DESC,"
        " created_at DESC LIMIT 50",
        ("home",),
    )
    assert "idx_gallery_items_sha256" in _plan(
        c,
        "SELECT id FROM gallery_items WHERE content_sha256=?",
        (_SHA,),
    )
    assert "idx_gallery_items_trash" in _plan(
        c,
        "SELECT id FROM gallery_items WHERE deleted_at IS NOT NULL"
        " AND filename <> '' AND deleted_at < ?",
        ("2026-01-01 00:00:00",),
    )

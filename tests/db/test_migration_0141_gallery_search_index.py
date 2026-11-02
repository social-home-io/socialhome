"""Migration 0141 — gallery captions and album names in ``search_index``
(D11): the backfill indexes what is already stored, and triggers keep the
index in step with every write to ``gallery_items`` / ``gallery_albums``."""

from __future__ import annotations

import sqlite3

from tests.db.connections import connect
from tests.migration_chain import apply_through

_VERSION = 141


def _fresh(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def _album(c, album_id: str, name: str, description: str | None = None) -> None:
    c.execute(
        "INSERT INTO gallery_albums(id, name, description) VALUES(?, ?, ?)",
        (album_id, name, description),
    )


def _item(c, item_id: str, album_id: str, caption: str | None) -> None:
    c.execute(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type,"
        " filename, thumbnail_filename, width, height, caption)"
        " VALUES(?, ?, 'u-a', 'photo', 'f.webp', 't.webp', 1, 1, ?)",
        (item_id, album_id, caption),
    )


def _index(c, scope: str) -> dict[str, tuple[str | None, str, str]]:
    return {
        r["ref_id"]: (r["space_id"], r["title"], r["body"])
        for r in c.execute(
            "SELECT ref_id, space_id, title, body FROM search_index WHERE scope=?",
            (scope,),
        )
    }


def _match(c, scope: str, phrase: str) -> list[str]:
    return sorted(
        r[0]
        for r in c.execute(
            "SELECT ref_id FROM search_index WHERE search_index MATCH ? AND scope=?",
            (phrase, scope),
        )
    )


def test_backfill_indexes_stored_captions_and_names(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION - 1)
    _album(c, "al-1", "Sommer", "am Meer")
    _item(c, "i-1", "al-1", "Sandburg")
    _item(c, "i-2", "al-1", None)
    _item(c, "i-3", "al-1", "   ")

    apply_through(c, _VERSION)

    assert _index(c, "gallery_item") == {"i-1": (None, "", "Sandburg")}
    assert _index(c, "gallery_album") == {"al-1": (None, "Sommer", "am Meer")}
    assert _match(c, "gallery_album", '"meer"*') == ["al-1"]


def test_triggers_follow_every_write(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    _album(c, "al-1", "Urlaub")
    _album(c, "al-2", "Garten")
    _item(c, "i-1", "al-1", "Sandburg")
    _item(c, "i-2", "al-1", None)
    assert set(_index(c, "gallery_item")) == {"i-1"}

    # A caption added, changed, cleared.
    c.execute("UPDATE gallery_items SET caption='Muschel' WHERE id='i-2'")
    c.execute("UPDATE gallery_items SET caption='Leuchtturm' WHERE id='i-1'")
    assert _match(c, "gallery_item", '"sandburg"') == []
    assert _match(c, "gallery_item", '"leuchtturm"') == ["i-1"]
    c.execute("UPDATE gallery_items SET caption=NULL WHERE id='i-2'")
    assert set(_index(c, "gallery_item")) == {"i-1"}

    # Moved to another album: still one row.
    c.execute("UPDATE gallery_items SET album_id='al-2' WHERE id='i-1'")
    assert set(_index(c, "gallery_item")) == {"i-1"}

    # Trash keeps the row (the search reads deleted_at), purge drops it.
    c.execute("UPDATE gallery_items SET deleted_at=datetime('now') WHERE id='i-1'")
    assert set(_index(c, "gallery_item")) == {"i-1"}
    c.execute("DELETE FROM gallery_items WHERE id='i-1'")
    assert _index(c, "gallery_item") == {}

    # An album renamed, then gone with its items (FK cascade fires too).
    _item(c, "i-3", "al-1", "Eis")
    c.execute("UPDATE gallery_albums SET name='Ferien' WHERE id='al-1'")
    assert _match(c, "gallery_album", '"urlaub"') == []
    assert _match(c, "gallery_album", '"ferien"') == ["al-1"]
    c.execute("DELETE FROM gallery_albums WHERE id='al-1'")
    assert set(_index(c, "gallery_album")) == {"al-2"}
    assert _index(c, "gallery_item") == {}


def test_a_tombstone_leaves_the_index(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    _album(c, "al-1", "Urlaub", "Kroatien")
    _item(c, "i-1", "al-1", "Sandburg")
    # What the 0085 tombstone writes: no name, no caption.
    c.execute(
        "UPDATE gallery_items SET deleted_at=datetime('now'), filename='',"
        " caption=NULL WHERE id='i-1'"
    )
    c.execute(
        "UPDATE gallery_albums SET deleted_at=datetime('now'), name='',"
        " description=NULL WHERE id='al-1'"
    )
    assert _index(c, "gallery_item") == {}
    assert _index(c, "gallery_album") == {}

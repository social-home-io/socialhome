"""Migration 0046 — drop the local-user FKs on the gallery tables (#650).

The rebuild is the dangerous kind: with foreign-key enforcement on,
``DROP TABLE gallery_albums`` implicitly deletes every row and cascades
into ``gallery_items``, which would destroy the user's whole gallery. These
tests pin both that it preserves data and that it refuses to run when the
pragma it depends on hasn't taken effect.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_MIG = (
    Path(__file__).resolve().parents[2]
    / "socialhome"
    / "migrations"
    / "0046_gallery_remote_owner.py"
)


def _load_migrate():
    spec = importlib.util.spec_from_file_location("mig0046", _MIG)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


#: The pre-0046 shape, verbatim from 0001 — the FKs are the point.
_OLD_SCHEMA = """
CREATE TABLE users (user_id TEXT PRIMARY KEY, username TEXT);
CREATE TABLE spaces (id TEXT PRIMARY KEY);
CREATE TABLE gallery_albums (
    id              TEXT PRIMARY KEY,
    space_id        TEXT REFERENCES spaces(id) ON DELETE CASCADE
                    CHECK(space_id IS NULL OR space_id <> '__household__'),
    retention_exempt INTEGER NOT NULL DEFAULT 0 CHECK(retention_exempt IN (0,1)),
    is_system       INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    owner_user_id   TEXT REFERENCES users(user_id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    cover_item_id   TEXT,
    item_count      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE gallery_items (
    id                  TEXT PRIMARY KEY,
    album_id            TEXT NOT NULL REFERENCES gallery_albums(id) ON DELETE CASCADE,
    uploaded_by         TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    item_type           TEXT NOT NULL CHECK(item_type IN ('photo','video')),
    filename            TEXT NOT NULL,
    thumbnail_filename  TEXT NOT NULL,
    width               INTEGER NOT NULL,
    height              INTEGER NOT NULL,
    duration_s          REAL,
    caption             TEXT,
    taken_at            TEXT,
    sort_order          INTEGER NOT NULL DEFAULT 0,
    source_post_id      TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript(_OLD_SCHEMA)
    conn.execute("INSERT INTO users VALUES ('u-local', 'alice')")
    conn.execute("INSERT INTO spaces VALUES ('sp-1')")
    conn.execute(
        "INSERT INTO gallery_albums (id, space_id, is_system, owner_user_id, name)"
        " VALUES ('al-user', 'sp-1', 0, 'u-local', 'Holiday')"
    )
    conn.execute(
        "INSERT INTO gallery_albums (id, space_id, is_system, owner_user_id, name)"
        " VALUES ('al-sys', 'sp-1', 1, NULL, 'Posts')"
    )
    for n, album in ((1, "al-user"), (2, "al-user"), (3, "al-sys")):
        conn.execute(
            "INSERT INTO gallery_items (id, album_id, uploaded_by, item_type,"
            " filename, thumbnail_filename, width, height)"
            f" VALUES ('it-{n}', '{album}', 'u-local', 'photo',"
            f" 'f{n}.webp', 't{n}.webp', 10, 10)"
        )


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    _seed(c)
    yield c
    c.close()


def test_old_schema_really_rejects_a_remote_owner(conn):
    """Baseline — this IS the bug, so prove it exists before fixing it.

    A synced album is owned by a user on another household, who has no
    local ``users`` row. Without this failing first, the migration proves
    nothing.
    """
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gallery_albums (id, space_id, is_system,"
            " owner_user_id, name)"
            " VALUES ('al-remote', 'sp-1', 0, 'u-on-another-household', 'Theirs')"
        )


def test_migration_preserves_every_row(conn):
    """The whole gallery must survive the rebuild.

    With enforcement on, dropping the parent cascades into the children —
    so a silent failure here looks like "the user's gallery vanished".
    """
    before_albums = {r["id"] for r in conn.execute("SELECT id FROM gallery_albums")}
    before_items = {r["id"] for r in conn.execute("SELECT id FROM gallery_items")}

    _load_migrate()(conn)

    assert {
        r["id"] for r in conn.execute("SELECT id FROM gallery_albums")
    } == before_albums
    assert {
        r["id"] for r in conn.execute("SELECT id FROM gallery_items")
    } == before_items
    # Column values, not just row ids.
    row = conn.execute(
        "SELECT owner_user_id, name, is_system FROM gallery_albums WHERE id='al-user'"
    ).fetchone()
    assert (row["owner_user_id"], row["name"], row["is_system"]) == (
        "u-local",
        "Holiday",
        0,
    )
    assert (
        conn.execute(
            "SELECT uploaded_by FROM gallery_items WHERE id='it-1'"
        ).fetchone()["uploaded_by"]
        == "u-local"
    )


def test_migration_allows_a_remote_owner_afterwards(conn):
    """The point of the change: a synced album/item now inserts."""
    _load_migrate()(conn)

    conn.execute(
        "INSERT INTO gallery_albums (id, space_id, is_system, owner_user_id, name)"
        " VALUES ('al-remote', 'sp-1', 0, 'u-on-another-household', 'Theirs')"
    )
    conn.execute(
        "INSERT INTO gallery_items (id, album_id, uploaded_by, item_type,"
        " filename, thumbnail_filename, width, height)"
        " VALUES ('it-remote', 'al-remote', 'u-on-another-household', 'photo',"
        " 'r.webp', 'rt.webp', 10, 10)"
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM gallery_albums WHERE id='al-remote'"
        ).fetchone()[0]
        == 1
    )


def test_migration_keeps_the_album_cascade(conn):
    """Only the *users* FKs go. Deleting an album must still take its items."""
    _load_migrate()(conn)
    conn.execute("PRAGMA foreign_keys=ON")

    conn.execute("DELETE FROM gallery_albums WHERE id='al-user'")

    remaining = {r["id"] for r in conn.execute("SELECT id FROM gallery_items")}
    assert remaining == {"it-3"}, "album_id cascade was lost in the rebuild"


def test_migration_keeps_the_system_album_uniqueness(conn):
    """The partial unique index must come back, or a race seats two."""
    _load_migrate()(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gallery_albums (id, space_id, is_system, owner_user_id,"
            " name) VALUES ('al-sys-2', 'sp-1', 1, NULL, 'Posts again')"
        )


class _PragmaIgnoringConn:
    """Forwards everything to a real connection, but reports foreign-key
    enforcement as still ON — i.e. the exact behaviour of a
    ``PRAGMA foreign_keys=OFF`` issued inside a transaction, where SQLite
    silently ignores it. ``sqlite3.Connection.execute`` is read-only, so a
    proxy is the only way to simulate this."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, *a, **kw):
        if sql.strip().upper() == "PRAGMA FOREIGN_KEYS":
            return self._real.execute("SELECT 1")
        return self._real.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_migration_refuses_to_run_with_enforcement_stuck_on(conn):
    """If ``PRAGMA foreign_keys=OFF`` doesn't stick, abort — don't guess.

    The pragma is silently ignored inside a transaction. Proceeding anyway
    would cascade-delete every gallery_items row, so the migration must
    fail loudly and leave the data untouched.
    """
    with pytest.raises(RuntimeError, match="foreign-key enforcement is active"):
        _load_migrate()(_PragmaIgnoringConn(conn))

    # Nothing was touched, and the old shape is still in place.
    assert conn.execute("SELECT count(*) FROM gallery_items").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM gallery_albums").fetchone()[0] == 2
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='gallery_albums'"
    ).fetchone()[0]
    assert "users(user_id)" in sql, "table was rebuilt despite the guard"

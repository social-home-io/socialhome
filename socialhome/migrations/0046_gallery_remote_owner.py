"""Drop the local-user FKs on ``gallery_albums`` / ``gallery_items`` (#650).

Cross-household gallery **metadata** never synced. A synced album or item is
owned by a user on another household, who by definition has no local ``users``
row, so these constraints made the INSERT impossible:

    gallery_albums.owner_user_id TEXT REFERENCES users(user_id) ON DELETE CASCADE
    gallery_items.uploaded_by    TEXT NOT NULL REFERENCES users(user_id) ...

The §25.6 receiver swallowed the resulting IntegrityError as "already exists",
so a member household ended up with the image *bytes* on disk (those ride a
durable retry outbox) and no rows at all. Directly-paired members were affected
too, not just mesh-joined ones.

Federated content elsewhere already models this correctly:
``space_posts.author`` is a bare ``TEXT NOT NULL`` with no FK, commented
"user_id or 'system-integration'", precisely because the author may be remote.
This brings gallery in line.

Why dropping the constraint is safe rather than merely convenient: the
``ON DELETE CASCADE`` it carries is **unreachable today**. Nothing in the
codebase hard-deletes a ``users`` row — ``UserService.deprovision`` only
soft-deletes (``state='inactive'`` plus ``deleted_at`` / ``grace_until``), and
no purge job consumes ``grace_until``. So no cascade can currently fire, and
removing it changes no local behaviour. Nothing JOINs either column either;
both are read straight back out as provenance and rendered.

Written as a Python migration rather than SQL because SQLite can only drop a
constraint by rebuilding the table, and that is genuinely dangerous here:
with ``PRAGMA foreign_keys=ON`` (which ``AsyncDatabase`` sets *before* running
migrations), ``DROP TABLE gallery_albums`` performs an implicit delete of every
row, which cascades into ``gallery_items`` and would destroy the user's entire
gallery. ``PRAGMA foreign_keys`` is also a silent no-op inside a transaction.
So this migration asserts the pragma actually took effect, and verifies the
row counts survive, aborting before any destructive step if either check fails.
"""

from __future__ import annotations

import sqlite3

#: Rebuilt DDL — byte-identical to 0001 except the two ``REFERENCES
#: users(user_id) ON DELETE CASCADE`` clauses are gone.
_ALBUMS_NEW = """
CREATE TABLE gallery_albums_new (
    id              TEXT PRIMARY KEY,
    space_id        TEXT REFERENCES spaces(id) ON DELETE CASCADE
                    CHECK(space_id IS NULL OR space_id <> '__household__'),
    retention_exempt INTEGER NOT NULL DEFAULT 0
                    CHECK(retention_exempt IN (0,1)),
    is_system       INTEGER NOT NULL DEFAULT 0
                    CHECK(is_system IN (0,1)),
    -- No FK: the owner may be a user on another household (#650). NULL
    -- still means "no human owner" (the auto-mirrored system album).
    owner_user_id   TEXT,
    name            TEXT NOT NULL,
    description     TEXT,
    cover_item_id   TEXT,
    item_count      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_ITEMS_NEW = """
CREATE TABLE gallery_items_new (
    id                  TEXT PRIMARY KEY,
    album_id            TEXT NOT NULL REFERENCES gallery_albums(id) ON DELETE CASCADE,
    -- No FK: the uploader may live on another household (#650).
    uploaded_by         TEXT NOT NULL,
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
)
"""

#: Recreated verbatim from 0001 after the swap.
_INDEXES = (
    (
        "CREATE INDEX IF NOT EXISTS idx_gallery_albums_space"
        "   ON gallery_albums(space_id, created_at DESC)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_gallery_albums_owner"
        "   ON gallery_albums(owner_user_id) WHERE owner_user_id IS NOT NULL"
    ),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_gallery_albums_system_unique"
        "   ON gallery_albums(COALESCE(space_id, '__household__')) WHERE is_system = 1"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_gallery_items_album"
        "   ON gallery_items(album_id, sort_order, created_at DESC)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_gallery_items_uploader"
        "   ON gallery_items(uploaded_by)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_gallery_items_source_post"
        "   ON gallery_items(source_post_id) WHERE source_post_id IS NOT NULL"
    ),
)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> None:
    albums_before = _count(conn, "gallery_albums")
    items_before = _count(conn, "gallery_items")

    # Rebuilding a parent table with FK enforcement ON would cascade-delete
    # every child row. Disable, then PROVE it: the pragma is silently
    # ignored inside a transaction, and a silent no-op here costs the user
    # their whole gallery.
    conn.execute("PRAGMA foreign_keys=OFF")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise RuntimeError(
            "0046: refusing to rebuild gallery tables while foreign-key "
            "enforcement is active (PRAGMA foreign_keys=OFF did not take "
            "effect — most likely this ran inside a transaction). Dropping "
            "gallery_albums now would cascade-delete every gallery_items row."
        )
    try:
        # Children first: gallery_items has no dependants, so rebuilding it
        # is unobservable, and doing it before the parent swap keeps the
        # album_id references pointing at a table that always exists.
        conn.execute(_ITEMS_NEW)
        conn.execute(
            "INSERT INTO gallery_items_new SELECT id, album_id, uploaded_by,"
            " item_type, filename, thumbnail_filename, width, height,"
            " duration_s, caption, taken_at, sort_order, source_post_id,"
            " created_at FROM gallery_items"
        )
        conn.execute("DROP TABLE gallery_items")
        conn.execute("ALTER TABLE gallery_items_new RENAME TO gallery_items")

        conn.execute(_ALBUMS_NEW)
        conn.execute(
            "INSERT INTO gallery_albums_new SELECT id, space_id,"
            " retention_exempt, is_system, owner_user_id, name, description,"
            " cover_item_id, item_count, created_at, updated_at"
            " FROM gallery_albums"
        )
        conn.execute("DROP TABLE gallery_albums")
        conn.execute("ALTER TABLE gallery_albums_new RENAME TO gallery_albums")

        for stmt in _INDEXES:
            conn.execute(stmt)

        albums_after = _count(conn, "gallery_albums")
        items_after = _count(conn, "gallery_items")
        if (albums_after, items_after) != (albums_before, items_before):
            raise RuntimeError(
                f"0046: row count changed during rebuild — albums "
                f"{albums_before}->{albums_after}, items "
                f"{items_before}->{items_after}; rolling back"
            )
        # Re-checks every remaining constraint (notably gallery_items.album_id
        # against the freshly-swapped parent) now that enforcement is off.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"0046: rebuild left {len(violations)} foreign-key "
                f"violation(s): {violations[:5]}"
            )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

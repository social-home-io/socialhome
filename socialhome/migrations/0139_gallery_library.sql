-- 0139 — The household gallery's library model: a 30-day trash, duplicate
-- detection, private-album audiences, per-person favourites and in-album
-- date order. The one migration of the gallery redesign (owner decisions
-- 2026-11-02: albums-first, a photo lives in exactly one album; storage
-- stays WebP ≤ 2560 px / WebM ≤ 1920 px, ``domain/media_constraints.py``).
--
-- 1. ``gallery_items.content_sha256`` — SHA-256 (64 lowercase hex) of the
--    bytes as uploaded, before conversion to WebP / WebM, so a second
--    upload of the same original is recognised although every stored file
--    is a fresh re-encode. Only the digest is kept, never the uploaded
--    bytes; it never leaves the server and is not in any API. NULL for
--    every row before this migration, for post mirrors and for federated
--    space items. Partial index for the lookup.
-- 2. Trash: a deleted household item keeps its row and its files with
--    ``deleted_at`` / ``deleted_by`` set (the 0085 columns, reused — a
--    space tombstone is the same columns with the files blanked). The
--    partial index ``idx_gallery_items_trash`` serves the trash list and
--    the purge of rows older than 30 days; space tombstones (files blanked)
--    stay out of it.
-- 3. ``gallery_albums.audience`` — 'everyone' (default, every existing
--    row) or 'chosen' (a private album: only its owner and the people in
--    ``gallery_album_audience``). Only a household album may be 'chosen'
--    (CHECK), a space album belongs to its space.
-- 4. ``gallery_album_audience(album_id, user_id)`` — the people of a
--    private album; both sides are local rows, so both are FKs.
-- 5. ``gallery_favourites(user_id, item_id)`` — private per-person
--    favourites; they go with the item when the trash purges it.
-- 6. ``idx_gallery_items_album_taken`` — live items of an album by the day
--    taken (the upload day when the photo carries none), newest first.
--
-- "Leaving the household keeps photos" needs no schema change: 0046
-- already dropped the ``REFERENCES users(user_id) ON DELETE CASCADE`` on
-- ``gallery_albums.owner_user_id`` and ``gallery_items.uploaded_by`` (a
-- synced row's owner lives on another household and has no local
-- ``users`` row, so a ``SET NULL`` FK would refuse those inserts too). No
-- delete of a ``users`` row reaches a photo; ``GalleryService`` hands the
-- albums of a removed person to an admin.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data.
--       ``grep -rn "gallery_items\|gallery_albums" socialhome``: all SQL is
--       in ``repositories/gallery_repo.py``, plus ``media_reference_repo``
--       (reads ``filename`` / ``thumbnail_filename`` of every row — a
--       trashed row keeps its files, so the orphan sweep keeps them too),
--       ``data_export_service`` (live rows only, ``deleted_at IS NULL``)
--       and the §25.6 exporters / receiver / resume (space rows only;
--       ``space_id IS NOT NULL`` joins). Every live read already skips
--       ``deleted_at IS NOT NULL``, so a trashed item is absent everywhere
--       until restored. The 0085 album trigger tombstones only items still
--       live, so trashing a household album's items before the album keeps
--       their files. The 0086 ``sync_seq`` triggers list their columns
--       explicitly; the new columns are household-only and not in them.
--       ``backup_service`` does not dump gallery tables.
--   (2) Non-migration alternatives considered and rejected:
--       * Trash: new ``trashed_at`` columns — 0085's ``deleted_at`` /
--         ``deleted_by`` already mean "deleted by whom, when" and every
--         read skips them; reused (CLAUDE.md: no new column when an
--         existing one proves it). A separate trash table — moves rows,
--         breaks favourites and the media reference scan.
--       * Duplicates: hashing the stored WebP — it differs per upload
--         (re-encode), so it can't recognise a re-upload; hashing at read
--         time — needs every original, which is never kept.
--       * Audience: a JSON list column — no FK, no index for "albums I'm
--         in", and a person's removal could not drop them.
--       * Favourites: a preferences JSON blob — unbounded, unindexed,
--         survives the item's purge.
--       * ``ON DELETE SET NULL`` FKs on the owner / uploader with a table
--         rebuild (the plan's first draft): 0046 removed those FKs on
--         purpose (#650); re-adding any FK would refuse every federated
--         row of a remote owner, and nothing hard-deletes a ``users`` row.
--   (3) Smallest possible change: two ADD COLUMNs (one NULL, one with a
--       constant default — metadata-only in SQLite, no table rewrite, no
--       backfill), two small new tables and four indexes. No existing row
--       changes.

ALTER TABLE gallery_items ADD COLUMN content_sha256 TEXT
    CHECK(content_sha256 IS NULL
          OR (length(content_sha256) = 64
              AND content_sha256 NOT GLOB '*[^0-9a-f]*'));

CREATE INDEX IF NOT EXISTS idx_gallery_items_sha256
    ON gallery_items(content_sha256) WHERE content_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_gallery_items_trash
    ON gallery_items(deleted_at)
 WHERE deleted_at IS NOT NULL AND filename <> '';

CREATE INDEX IF NOT EXISTS idx_gallery_items_album_taken
    ON gallery_items(
        album_id,
        COALESCE(taken_at, substr(created_at, 1, 10)) DESC,
        created_at DESC
    )
 WHERE deleted_at IS NULL;

ALTER TABLE gallery_albums ADD COLUMN audience TEXT NOT NULL DEFAULT 'everyone'
    CHECK(audience IN ('everyone', 'chosen')
          AND (audience = 'everyone' OR space_id IS NULL));

CREATE TABLE IF NOT EXISTS gallery_album_audience (
    album_id    TEXT NOT NULL REFERENCES gallery_albums(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (album_id, user_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_gallery_album_audience_user
    ON gallery_album_audience(user_id);

CREATE TABLE IF NOT EXISTS gallery_favourites (
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    item_id     TEXT NOT NULL REFERENCES gallery_items(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, item_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_gallery_favourites_item
    ON gallery_favourites(item_id);

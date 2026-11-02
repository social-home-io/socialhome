-- 0141 — Gallery search (owner decision D11): photo captions and album
-- names join the one full-text index the app already has (``search_index``,
-- FTS5, 0001), so the gallery is searched by the same tokenizer (unicode61,
-- diacritics folded) instead of a second index.
--
-- Two new scopes in the existing table — no new column, no new table:
--
--   * ``gallery_item``  — ref_id = the item, space_id = its album's space,
--                         body = the caption (only rows that have one).
--   * ``gallery_album`` — ref_id = the album, space_id = its space,
--                         title = the name, body = the description.
--
-- Triggers keep them in step with ``gallery_items`` / ``gallery_albums``
-- on every write path at once — an upload, an edit, a move, a feed post's
-- mirror into the "Posts" album, a space row synced from another
-- household, a tombstone (which blanks the caption / name) and the trash
-- purge — and the backfill below indexes what is already there. A row in
-- the trash stays indexed: the gallery search joins back to the live row
-- and leaves out ``deleted_at IS NOT NULL``, so a restore needs no
-- re-index.
--
-- Privacy: the generic ``GET /api/search`` never returns or counts these
-- scopes (a household album's row has no space id, and a private album's
-- audience is not in the index). Only ``GallerySearchService`` reads them,
-- and only through ``SqliteGalleryRepo.search_items``, which applies who
-- may see the album (``album_visible_to``), space seats, the trash and
-- blocks in the same SQL.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data.
--       ``search_index``: written only by ``SqliteSearchRepo.upsert`` /
--       ``delete`` (fed by ``SearchService`` bus handlers) and read by its
--       ``search`` / ``count_by_scope``; 0049 lists it only to skip virtual
--       tables. ``gallery_items`` / ``gallery_albums``: every SQL write is
--       in ``repositories/gallery_repo.py`` (create, update, move, trash,
--       restore, purge, tombstone, the §25.6 sync receiver), plus the 0085
--       album-tombstone trigger that blanks its items' captions — all of
--       them fire these triggers. ``backup_service`` dumps neither table;
--       ``data_export_service`` reads only the gallery tables.
--   (2) Non-migration alternatives considered and rejected:
--       * Index from bus events in ``SearchService`` (how posts are
--         indexed): the gallery events carry ids only, the sync receiver
--         and the 0085 tombstone trigger write without an event, and the
--         captions already stored would never be indexed — a search that
--         silently misses photos.
--       * A rebuild of the gallery rows at every start: rewrites the index
--         on each boot and still misses every change until the next one.
--       * ``LIKE`` over ``gallery_items.caption`` at query time: a full
--         scan per search, no diacritic folding, and a second search
--         behaviour next to the app's FTS5 one.
--       * A separate gallery FTS table: the parallel index this reuses
--         ``search_index`` to avoid.
--   (3) Smallest possible change: six triggers and one backfill INSERT into
--       the existing table. No column, no table, no existing row changes.

-- Backfill: what is already stored.
INSERT INTO search_index(scope, ref_id, space_id, title, body)
SELECT 'gallery_item', i.id, a.space_id, '', i.caption
  FROM gallery_items i JOIN gallery_albums a ON a.id = i.album_id
 WHERE trim(COALESCE(i.caption, '')) <> '';

INSERT INTO search_index(scope, ref_id, space_id, title, body)
SELECT 'gallery_album', a.id, a.space_id, a.name, COALESCE(a.description, '')
  FROM gallery_albums a
 WHERE trim(a.name || COALESCE(a.description, '')) <> '';

-- Items: a caption in, a caption changed or moved, a row gone.
CREATE TRIGGER IF NOT EXISTS gallery_items_search_insert
AFTER INSERT ON gallery_items
WHEN trim(COALESCE(NEW.caption, '')) <> ''
BEGIN
    INSERT INTO search_index(scope, ref_id, space_id, title, body)
    VALUES ('gallery_item', NEW.id,
            (SELECT space_id FROM gallery_albums WHERE id = NEW.album_id),
            '', NEW.caption);
END;

CREATE TRIGGER IF NOT EXISTS gallery_items_search_update
AFTER UPDATE OF caption, album_id ON gallery_items
WHEN COALESCE(OLD.caption, '') IS NOT COALESCE(NEW.caption, '')
  OR OLD.album_id IS NOT NEW.album_id
BEGIN
    DELETE FROM search_index WHERE scope = 'gallery_item' AND ref_id = OLD.id;
    INSERT INTO search_index(scope, ref_id, space_id, title, body)
    SELECT 'gallery_item', NEW.id,
           (SELECT space_id FROM gallery_albums WHERE id = NEW.album_id),
           '', NEW.caption
     WHERE trim(COALESCE(NEW.caption, '')) <> '';
END;

CREATE TRIGGER IF NOT EXISTS gallery_items_search_delete
AFTER DELETE ON gallery_items
WHEN trim(COALESCE(OLD.caption, '')) <> ''
BEGIN
    DELETE FROM search_index WHERE scope = 'gallery_item' AND ref_id = OLD.id;
END;

-- Albums: a name in, renamed or re-described, a row gone.
CREATE TRIGGER IF NOT EXISTS gallery_albums_search_insert
AFTER INSERT ON gallery_albums
WHEN trim(NEW.name || COALESCE(NEW.description, '')) <> ''
BEGIN
    INSERT INTO search_index(scope, ref_id, space_id, title, body)
    VALUES ('gallery_album', NEW.id, NEW.space_id, NEW.name,
            COALESCE(NEW.description, ''));
END;

CREATE TRIGGER IF NOT EXISTS gallery_albums_search_update
AFTER UPDATE OF name, description ON gallery_albums
WHEN OLD.name IS NOT NEW.name
  OR COALESCE(OLD.description, '') IS NOT COALESCE(NEW.description, '')
BEGIN
    DELETE FROM search_index WHERE scope = 'gallery_album' AND ref_id = OLD.id;
    INSERT INTO search_index(scope, ref_id, space_id, title, body)
    SELECT 'gallery_album', NEW.id, NEW.space_id, NEW.name,
           COALESCE(NEW.description, '')
     WHERE trim(NEW.name || COALESCE(NEW.description, '')) <> '';
END;

CREATE TRIGGER IF NOT EXISTS gallery_albums_search_delete
AFTER DELETE ON gallery_albums
BEGIN
    DELETE FROM search_index WHERE scope = 'gallery_album' AND ref_id = OLD.id;
END;

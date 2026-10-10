-- 0091 — One system-album item per (source post, file).
--
-- ``GalleryService.mirror_post`` mirrors a post's media into the space's (or
-- household's) system "Posts" album: it reads the post's mirror rows, and
-- when their URLs differ from the post's, deletes them and inserts fresh
-- ones under new random ids. Two mirrors of the same post running at once —
-- the live ``SPACE_POST_CREATED`` and the same post arriving by a §25.6 sync
-- (``SpacePostSynced``), or two overlapping sync sessions — both read "no
-- rows yet" and both insert: every photo shows twice in the Posts album,
-- and the URL-set idempotency check never repairs it (the URL *set* still
-- matches).
--
-- 1. Repair: delete every live mirror row (``source_post_id`` set,
--    ``deleted_at`` NULL) that has an older live twin — same source post,
--    same ``filename`` — keeping the oldest (``created_at``, then rowid),
--    and recount the system albums' ``item_count`` from their live rows.
-- 2. Guard: the partial unique index ``idx_gallery_items_post_mirror`` on
--    ``(source_post_id, filename)`` for live mirror rows. The repo's insert
--    (``create_item``) is already ``ON CONFLICT DO NOTHING``, so a racing
--    mirror's second insert is a no-op, not an error. A tombstoned mirror
--    row (``deleted_at`` set) is outside the index, so a re-mirror after it
--    is not blocked.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every path writing ``gallery_items.source_post_id``:
--       only ``GalleryService.mirror_post`` (via ``SqliteGalleryRepo
--       .create_item``) sets it; ``delete_items_by_source_post`` hard-deletes
--       a post's mirrors (``unmirror_post``); uploads, the federation
--       inbound item handler and the §25.6 receiver insert with
--       ``source_post_id`` NULL (the exporter never streams mirrors). Every
--       read that lists an album's items, ``recount_items`` and the media
--       reference scan go through the repo. The 0085 album trigger
--       tombstones items in place — outside this partial index.
--   (2) Non-migration alternatives considered and rejected: a lock or a
--       per-post serialising queue in the service (the two mirrors can run
--       in different code paths and the race window spans two awaited
--       repo calls; a process-local lock does not survive a future second
--       worker, and the owner wants the invariant in the database);
--       re-reading after insert and deleting the loser (still racy, and
--       leaves the duplicates already stored); deterministic item ids
--       derived from (post, url) (changes the id scheme every client and
--       the media reference scan know, and still needs the repair).
--   (3) Smallest change: a delete of duplicate rows only, a recount of the
--       derived ``item_count`` of system albums, and one partial index. No
--       column, no rewrite of any surviving row.

DELETE FROM gallery_items
 WHERE source_post_id IS NOT NULL
   AND deleted_at IS NULL
   AND EXISTS (
        SELECT 1 FROM gallery_items older
         WHERE older.source_post_id = gallery_items.source_post_id
           AND older.filename = gallery_items.filename
           AND older.deleted_at IS NULL
           AND (older.created_at < gallery_items.created_at
                OR (older.created_at = gallery_items.created_at
                    AND older.rowid < gallery_items.rowid))
   );

UPDATE gallery_albums
   SET item_count = (
        SELECT COUNT(*) FROM gallery_items i
         WHERE i.album_id = gallery_albums.id AND i.deleted_at IS NULL
   )
 WHERE is_system = 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_gallery_items_post_mirror
    ON gallery_items(source_post_id, filename)
 WHERE source_post_id IS NOT NULL AND deleted_at IS NULL;

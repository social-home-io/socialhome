-- 0142 — ``space_media_refs``: where a space file this household holds no
-- copy of lives in a GFS media cache.
--
-- A household reached only through a GFS (a member seated from an invite
-- link, a follower of a global space) gets a space item's pictures as a
-- ``media_refs`` descriptor inside the item's encrypted payload, not as
-- bytes: ``{"<local filename>": {blob_id, suite, epoch, size}}``. The file
-- is fetched from the household's GFS on arrival (prefetch) or when first
-- viewed (``GET /api/media/{filename}``), decrypted with the epoch's space
-- key and written under the same local filename — so this row is what the
-- media route reads to know where a missing file lives.
--
-- CLAUDE.md "audit before a migration":
--
-- 1. Audited every path that touches space media: ``space_media_outbox``
--    (sender-side push queue to paired / mesh members, keyed by target —
--    nothing a receiver keeps), ``SPACE_MEDIA_BLOB`` inbound (writes bytes,
--    keeps no row), the item tables that reference a file
--    (``space_posts.media_url`` / ``image_urls``, ``space_calendar_events``
--    cover, ``gallery_items``) and ``routes/media.py`` (serves by filename
--    only). None records where a file comes from.
-- 2. Alternatives rejected: a JSON column on each item table (three
--    migrations, and the media route would have to search three tables by
--    filename); keeping descriptors in memory (a file first viewed after a
--    restart could never be fetched); re-deriving them on view (needs the
--    sender's file). The filename is already unique per household
--    (``<uuid-hex>.<ext>``), so it is the natural key.
-- 3. Smallest change: one new table, additive; no existing row touched; the
--    rows go with their space (``ON DELETE CASCADE``).

CREATE TABLE IF NOT EXISTS space_media_refs (
    filename    TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    blob_id     TEXT NOT NULL,
    suite       TEXT NOT NULL,
    epoch       INTEGER NOT NULL CHECK (epoch >= 0),
    size        INTEGER NOT NULL CHECK (size > 0),
    -- The GFS scope the blob was uploaded under and how its address binds
    -- it (``sm1``): the reader re-derives the address with the scope id it
    -- knows itself — the space id, or its own channel id.
    scope       TEXT NOT NULL CHECK (scope IN ('space', 'channel')),
    addr_suite  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_space_media_refs_space
    ON space_media_refs(space_id);

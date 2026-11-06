-- GFS 0025 — the space media cache. Follows 0024 (moment delete log).
--
-- A household reached only through this server — a space member seated from
-- an invite link (envelope relay only, 191 KiB sealed cap) or a follower of a
-- public / global space — never received a space's pictures: the item names
-- a file on the host household, which that household can't reach. Now a
-- household encrypts each space file once (under the space content key,
-- padded to a size bucket) and uploads the ciphertext once per GFS; readers
-- fetch it here by its SHA-256. This server can't open it, and keeps it only
-- as a CACHE: a short life (``[space_media] ttl_seconds``, 3 days), a total
-- byte cap with least-recently-fetched eviction, a per-space cap.
--
-- CLAUDE.md "audit before a migration":
-- 1. Audited every store that holds bytes here: ``gfs_moment_media`` (the
--    closest — but keyed and capped per AUTHOR, cascading with the author's
--    registration, kept as long as a moment lives, up to 31 days, never
--    evicted: a share link must keep working), ``gfs_user_pictures`` (one
--    public plaintext avatar per user), ``gfs_envelope_queue`` (small sealed
--    envelopes per recipient, deleted on delivery) and
--    ``gfs_moment_key_mailbox`` (one sealed key per author × household).
--    None is keyed per space, short-lived and evictable.
-- 2. Alternatives rejected: reusing ``gfs_moment_media`` (its
--    ``author_user_id`` is a NOT NULL foreign key to a Momentum registration
--    — space uploads have no author here and must not name one — and its
--    quota, lifetime and delete rules are the author's; bending them would
--    change moments); carrying the bytes in the space item (every item would
--    carry up to 8 MB through each follower's socket and queue, and the
--    envelope relay caps a sealed frame at 191 KiB); keeping the index in
--    memory (four nodes share one data dir — each must serve what another
--    took, and a restart must not orphan files); no metadata at all, files
--    only (expiry, per-space quota and LRU eviction would need a directory
--    scan per upload).
-- 3. Smallest change: one table of metadata, the bytes are files under
--    ``media_dir/spaces/`` named by their SHA-256. No foreign key to
--    ``global_spaces``: a cascade would drop rows and orphan their files;
--    rows and files go together through the store (expiry sweep, eviction,
--    the space authority's delete), and nothing lives past the TTL.
CREATE TABLE IF NOT EXISTS gfs_space_media (
    -- sha256 of the blob, lowercase hex — the blob's address and file name.
    blob_id          TEXT PRIMARY KEY,
    -- The space whose authority (or writer) uploaded it; quota + delete key.
    space_id         TEXT NOT NULL,
    -- Ciphertext bytes (a padded bucket + nonce + tag).
    size             INTEGER NOT NULL CHECK(size > 0),
    -- Unix seconds.
    created_at       INTEGER NOT NULL,
    -- Unix seconds; never beyond created_at + the operator's TTL.
    expires_at       INTEGER NOT NULL,
    -- Unix seconds of the last fetch, moved at most once an hour per blob;
    -- the upload counts as one, so a fresh blob is not evicted first.
    last_fetched_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_expires
    ON gfs_space_media(expires_at);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_space
    ON gfs_space_media(space_id, created_at);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_lru
    ON gfs_space_media(last_fetched_at, created_at);

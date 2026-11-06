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
-- byte cap and a per-scope cap, both with least-recently-fetched eviction,
-- and an upload rate per scope.
-- A scope is a listed space, or a private space's opaque channel (v_51) —
-- so invite-link members of a private space get its pictures too, while
-- this server still knows that space only as a random channel id.
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
--    envelope relay caps a sealed frame at 191 KiB); keeping the index or the
--    upload counts in memory (four nodes share one data dir — each must serve
--    what another took and count what another accepted, and a restart must
--    not orphan files or reset a rate); no metadata at all, files only
--    (expiry, per-scope caps and LRU eviction would need a directory scan per
--    upload); counting the upload rate from the blob rows (eviction and
--    delete remove rows, so a scope could upload without bound by evicting
--    or deleting its own blobs); one owner scope per blob (whoever uploaded a
--    blob first would own it — a party that saw a public blob could pre-claim
--    it under its own space, then delete it from under the real one).
-- 3. Smallest change, all new tables, no change to an existing one:
--    * ``gfs_space_media`` — one row per stored blob (the bytes are files
--      under ``media_dir/spaces/`` named by their SHA-256);
--    * ``gfs_space_media_claims`` — which scopes hold each blob; identical
--      bytes uploaded for another scope add a claim, never a copy, and a
--      blob's file goes only when its last claim does;
--    * ``gfs_space_media_uploads`` — accepted uploads per scope per hour,
--      the rate limit's own count (two buckets live, the sweep drops older);
--    * ``gfs_space_media_totals`` — one row kept by triggers, so the total
--      the global cap checks on every upload is a primary-key read, not a
--      scan;
--    * ``gfs_space_media_sweep`` — one row: the orphan-file sweep's lease
--      (one node of a cluster scans the shared dir) and its name cursor.
--    One ``(scope_kind, scope_id)`` pair rather than a table per scope: the
--    cache, its caps and its eviction are the same for both. No foreign key
--    to ``global_spaces`` / ``gfs_channels``: a cascade would drop rows and
--    orphan their files; rows and files go together through the store
--    (expiry sweep, eviction, a seed holder's delete), the sweep removes any
--    file left without a row, and nothing lives past the TTL.
CREATE TABLE IF NOT EXISTS gfs_space_media (
    -- sha256 of the blob, lowercase hex — the blob's address and file name.
    blob_id          TEXT PRIMARY KEY,
    -- Ciphertext bytes (a padded bucket + nonce + tag).
    size             INTEGER NOT NULL CHECK(size > 0),
    -- Unix seconds.
    created_at       INTEGER NOT NULL,
    -- Unix seconds: the latest expiry of its claims, never beyond the
    -- operator's TTL from when that claim was made.
    expires_at       INTEGER NOT NULL,
    -- Unix seconds of the last fetch, moved at most once an hour per blob;
    -- the upload counts as one, so a fresh blob is not evicted first.
    last_fetched_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_expires
    ON gfs_space_media(expires_at);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_lru
    ON gfs_space_media(last_fetched_at, created_at);

CREATE TABLE IF NOT EXISTS gfs_space_media_claims (
    -- Strictly increasing per claim made (never reused — AUTOINCREMENT): an
    -- eviction or expiry sweep notes the highest before it reads its
    -- candidates and drops only claims up to it, so a claim another node
    -- makes meanwhile keeps its blob.
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    blob_id      TEXT NOT NULL,
    -- A listed space (``space`` + its id) or a private space's opaque v_51
    -- channel (``channel`` + the random channel id; this server never
    -- learns that channel's space). The quota and delete key.
    scope_kind   TEXT NOT NULL CHECK(scope_kind IN ('space', 'channel')),
    scope_id     TEXT NOT NULL,
    -- The blob's size, kept here so a scope's live bytes are an index read.
    size         INTEGER NOT NULL CHECK(size > 0),
    created_at   INTEGER NOT NULL,
    -- This scope's hold; never beyond created_at + the operator's TTL.
    expires_at   INTEGER NOT NULL,
    UNIQUE (blob_id, scope_kind, scope_id)
);
-- A scope's live bytes (covering: SUM(size) WHERE expires_at > now).
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_claims_scope
    ON gfs_space_media_claims(scope_kind, scope_id, expires_at, size);

CREATE TABLE IF NOT EXISTS gfs_space_media_uploads (
    scope_kind   TEXT NOT NULL CHECK(scope_kind IN ('space', 'channel')),
    scope_id     TEXT NOT NULL,
    -- unix seconds // 3600
    hour_bucket  INTEGER NOT NULL,
    count        INTEGER NOT NULL CHECK(count >= 0),
    PRIMARY KEY (scope_kind, scope_id, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_uploads_bucket
    ON gfs_space_media_uploads(hour_bucket);

CREATE TABLE IF NOT EXISTS gfs_space_media_totals (
    id      INTEGER PRIMARY KEY CHECK(id = 1),
    blobs   INTEGER NOT NULL,
    bytes   INTEGER NOT NULL
);
INSERT OR IGNORE INTO gfs_space_media_totals(id, blobs, bytes) VALUES(1, 0, 0);
CREATE TRIGGER IF NOT EXISTS trg_gfs_space_media_totals_ins
    AFTER INSERT ON gfs_space_media
BEGIN
    UPDATE gfs_space_media_totals
       SET blobs = blobs + 1, bytes = bytes + NEW.size WHERE id = 1;
END;
CREATE TRIGGER IF NOT EXISTS trg_gfs_space_media_totals_del
    AFTER DELETE ON gfs_space_media
BEGIN
    UPDATE gfs_space_media_totals
       SET blobs = blobs - 1, bytes = bytes - OLD.size WHERE id = 1;
END;

-- One row: which cluster node runs the orphan-file sweep right now (a short
-- lease — the nodes share one data dir, one scanner is enough) and where in
-- the directory's name order the last sweep stopped, so successive sweeps
-- walk the whole directory a bounded batch at a time.
CREATE TABLE IF NOT EXISTS gfs_space_media_sweep (
    id           INTEGER PRIMARY KEY CHECK(id = 1),
    holder       TEXT NOT NULL DEFAULT '',
    lease_until  INTEGER NOT NULL DEFAULT 0,
    cursor       TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO gfs_space_media_sweep(id) VALUES(1);

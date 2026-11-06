-- GFS 0025 — the space media cache. Follows 0024 (moment delete log).
--
-- A household reached only through this server — a space member seated from
-- an invite link (envelope relay only, 191 KiB sealed cap) or a follower of a
-- public / global space — never received a space's pictures: the item names
-- a file on the host household, which that household can't reach. Now a
-- household encrypts each space file once (under the space content key,
-- padded to a size bucket) and uploads the ciphertext once per GFS; readers
-- fetch it here by its address. This server can't open it, and keeps it only
-- as a CACHE: a short life (``[space_media] ttl_seconds``, 3 days), a total
-- byte cap and a per-scope cap, both with least-recently-fetched eviction
-- (fair across scopes), an upload rate per scope and a byte budget per
-- client address. A scope is a listed space, or a private space's opaque
-- channel (v_51) — so invite-link members of a private space get its
-- pictures too, while this server still knows that space only as a random
-- channel id.
--
-- The address is bound to the scope: sha256("sm1|" + scope_kind + "|" +
-- scope_id + "|" + ciphertext) (``domain/gfs_space_media.py``). The same
-- ciphertext has another address in every scope, so a blob belongs to
-- exactly ONE scope — no cross-scope claims, owners or tombstones (security
-- review v2, N1: with a bare SHA-256 a follower who fetched a blob could
-- first-store it on another GFS under its own scope and own it there).
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
--    quota, lifetime and delete rules are the author's); carrying the bytes
--    in the space item (every item would carry up to 8 MB through each
--    follower's socket and queue, and the envelope relay caps a sealed frame
--    at 191 KiB); keeping the index, the rates or the budgets in memory (four
--    nodes share one data dir — each must serve what another took and count
--    what another accepted, and a restart must not orphan files or reset a
--    rate); files only (expiry, caps and LRU eviction would need a directory
--    scan per upload); counting the upload rate from the blob rows (eviction
--    and delete remove rows, so a scope could upload without bound); a
--    content-only address with claims from several scopes (review rounds
--    1–3 and F4: squatting, then first-storer ownership — replaced by the
--    scope-bound address, which needs neither).
-- 3. Smallest change, all new tables, no change to an existing one:
--    * ``gfs_space_media`` — one row per blob, with its one scope (the
--      bytes are files under ``data_dir/space_media/``, never under the
--      public ``media/`` dir);
--    * ``gfs_space_media_scope_bytes`` — trigger-kept bytes per scope: the
--      per-scope cap and the fair eviction under the global cap read it;
--    * ``gfs_space_media_totals`` — one trigger-kept row, so the global cap
--      is a primary-key read, not a scan;
--    * ``gfs_space_media_uploads`` — accepted uploads per scope per hour;
--    * ``gfs_space_media_ip_bytes`` — bytes per client address per hour
--      (a keyed hash, never an address);
--    * ``gfs_space_media_sweep`` — one row: the orphan-file sweep's lease
--      and its name cursor.
--    No foreign key to ``global_spaces`` / ``gfs_channels``: a cascade would
--    drop rows and orphan their files; rows and files go together through
--    the store (expiry, eviction, a seed holder's delete, a ban), the sweep
--    removes any file left without a row, and nothing lives past the TTL.
CREATE TABLE IF NOT EXISTS gfs_space_media (
    -- The scope-bound address (lowercase hex) — also the file name.
    blob_id          TEXT PRIMARY KEY,
    -- A listed space (``space`` + its id) or a private space's opaque v_51
    -- channel (``channel`` + the random channel id; this server never
    -- learns that channel's space) — bound into the address.
    scope_kind       TEXT NOT NULL CHECK(scope_kind IN ('space', 'channel')),
    scope_id         TEXT NOT NULL,
    -- Ciphertext bytes (a padded bucket + nonce + tag).
    size             INTEGER NOT NULL CHECK(size > 0),
    -- Unix seconds.
    created_at       INTEGER NOT NULL,
    -- Unix seconds, never beyond created_at + the operator's TTL; a retry
    -- does not extend it.
    expires_at       INTEGER NOT NULL,
    -- Unix seconds of the last fetch, moved at most once an hour per blob;
    -- the upload counts as one, so a fresh blob is not evicted first.
    last_fetched_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_expires
    ON gfs_space_media(expires_at);
-- A scope's blobs, least recently fetched first (its own eviction, the
-- fair eviction, a ban's withdrawal).
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_scope
    ON gfs_space_media(scope_kind, scope_id, last_fetched_at, created_at);

-- Bytes each scope holds (expired ones count until the sweep drops them).
CREATE TABLE IF NOT EXISTS gfs_space_media_scope_bytes (
    scope_kind  TEXT NOT NULL,
    scope_id    TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    PRIMARY KEY (scope_kind, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_scope_bytes
    ON gfs_space_media_scope_bytes(bytes);

CREATE TABLE IF NOT EXISTS gfs_space_media_totals (
    id      INTEGER PRIMARY KEY CHECK(id = 1),
    blobs   INTEGER NOT NULL,
    bytes   INTEGER NOT NULL
);
INSERT OR IGNORE INTO gfs_space_media_totals(id, blobs, bytes) VALUES(1, 0, 0);
CREATE TRIGGER IF NOT EXISTS trg_gfs_space_media_ins
    AFTER INSERT ON gfs_space_media
BEGIN
    UPDATE gfs_space_media_totals
       SET blobs = blobs + 1, bytes = bytes + NEW.size WHERE id = 1;
    INSERT INTO gfs_space_media_scope_bytes(scope_kind, scope_id, bytes)
        VALUES(NEW.scope_kind, NEW.scope_id, NEW.size)
        ON CONFLICT(scope_kind, scope_id) DO UPDATE SET bytes = bytes + NEW.size;
END;
CREATE TRIGGER IF NOT EXISTS trg_gfs_space_media_del
    AFTER DELETE ON gfs_space_media
BEGIN
    UPDATE gfs_space_media_totals
       SET blobs = blobs - 1, bytes = bytes - OLD.size WHERE id = 1;
    UPDATE gfs_space_media_scope_bytes SET bytes = bytes - OLD.size
        WHERE scope_kind = OLD.scope_kind AND scope_id = OLD.scope_id;
    DELETE FROM gfs_space_media_scope_bytes
        WHERE scope_kind = OLD.scope_kind AND scope_id = OLD.scope_id
        AND bytes <= 0;
END;

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

-- Bytes uploaded per client address per hour — the backstop budget
-- (``[space_media] ip_bytes_per_hour``), cluster-wide in the shared DB.
-- ``ip_key`` is a keyed hash of the address bucket (an IPv6 /64), never the
-- address; two buckets live, the sweep drops older.
CREATE TABLE IF NOT EXISTS gfs_space_media_ip_bytes (
    ip_key       TEXT NOT NULL,
    hour_bucket  INTEGER NOT NULL,
    bytes        INTEGER NOT NULL CHECK(bytes >= 0),
    PRIMARY KEY (ip_key, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_ip_bytes_bucket
    ON gfs_space_media_ip_bytes(hour_bucket);

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

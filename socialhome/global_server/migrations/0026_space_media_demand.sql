-- GFS 0026 — demand for space blobs that left the cache (v_83, space media
-- on demand). Follows 0025 (the space media cache).
--
-- A blob leaves the cache when its TTL runs out or it is evicted; after that
-- every reader missed it forever. Now a reader that misses asks this server
-- (``POST /gfs/media/{blob_id}/wanted``) and the server asks ONE holder
-- household to send the same deterministic ciphertext — kept in the cache
-- again (a re-upload) or piped straight to the waiting reader(s). To decide
-- which, and to make it ONE re-upload per blob across the cluster, the
-- nodes share a little state per blob: when this cache stored it (so fresh
-- content goes back to the cache, old content streams), how often it was
-- asked lately (so popular content is cached), and who holds the re-upload
-- claim.
--
-- CLAUDE.md "audit before a migration":
-- 1. Audited every place that already knows a blob: ``gfs_space_media``
--    (rows are deleted with the file on expiry / eviction — exactly when
--    this state is needed; keeping them would make every cap, LRU and
--    sweep query filter "file present"), ``gfs_space_media_uploads`` (per
--    scope per hour, no blob) and ``gfs_space_media_ip_bytes`` (a keyed
--    hourly byte budget — REUSED, not duplicated, for the per-scope stream
--    bytes and the per-reader-prefix request budget under their own key
--    prefixes).
-- 2. Alternatives rejected: memory only (four nodes behind round-robin: a
--    blob's requests land on every node, so neither the count nor the one
--    re-upload claim would hold, and a restart would forget which content
--    is fresh); the reader telling the age (content metadata in the clear,
--    and a reader could lie to force caching); keeping expired rows in
--    ``gfs_space_media`` (changes every existing query and trigger).
-- 3. Smallest change: one new table, no change to an existing one. Rows are
--    bounded — one per blob that left the cache or was asked for, pruned
--    30 days after last touched (the sweep) — and hold no reader: a count,
--    three timestamps and the scope the blob already had.
CREATE TABLE IF NOT EXISTS gfs_space_media_demand (
    -- The scope-bound address the readers ask for.
    blob_id        TEXT NOT NULL,
    -- The scope the request named (a reader can name another scope for the
    -- same id: that is another row, never this one's counts or claim).
    scope_kind     TEXT NOT NULL CHECK(scope_kind IN ('space', 'channel')),
    scope_id       TEXT NOT NULL,
    -- Unix seconds the cache stored the copy that left (its created_at);
    -- NULL when this cache never stored it.
    stored_at      INTEGER,
    -- The request window: when it started and how many requests it holds.
    window_start   INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0 CHECK(requests >= 0),
    -- Until when one node holds the re-upload of this blob (cluster-wide:
    -- one re-upload per blob per window).
    claimed_until  INTEGER NOT NULL DEFAULT 0,
    -- Last write, for the 30-day prune.
    touched_at     INTEGER NOT NULL,
    PRIMARY KEY (blob_id, scope_kind, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_demand_touched
    ON gfs_space_media_demand(touched_at);

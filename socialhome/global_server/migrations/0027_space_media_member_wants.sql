-- GFS 0027 — space media wants offered to a listed space's members (the
-- member fallback of space media on demand). Follows 0026 (demand).
--
-- When a reader misses a blob of a LISTED space, the server asks the space's
-- owner household over its socket (0026). When the owner is offline, does
-- not answer in time or is over its budget, the want is now offered to the
-- space's other members — anonymously: a member long-polls
-- ``POST /gfs/media/member-pull`` signed by the space's writer GROUP key (no
-- household identity) and claims at most one want, which it serves to the
-- session's ticket. The session lives in ONE node's memory; the pull lands on
-- whichever node the balancer picks. So the nodes share the open wants here,
-- and the claim is one conditional update on the shared database: exactly
-- one member serves a blob (no wasted 8 MiB uploads).
--
-- CLAUDE.md "audit before a migration":
-- 1. Audited every place that already holds on-demand state: the session
--    (``space_media_on_demand.MediaSession``, one node's memory — a pull on
--    another node can't see it); ``gfs_space_media_demand`` (0026: one row
--    per (blob, scope) — two nodes stream one blob as two sessions with two
--    tickets, and its ``claimed_until`` is the cluster-wide RE-UPLOAD claim,
--    which a member claim must not conflate); the cluster relay (it forwards
--    socket frames to households' sockets, not to anonymous HTTP long-polls,
--    and has no atomic claim).
-- 2. Alternatives rejected: memory only (a pull on another node would never
--    see the want; four nodes behind round-robin make that the common case);
--    pushing the want over the members' sockets (names the households —
--    exactly what the fallback must not learn); reusing 0026 (five ADD
--    COLUMNs keyed per ticket on a per-blob table, and one claim column for
--    two meanings); the reader telling members directly (readers and members
--    never talk; the GFS stays the only meeting point).
-- 3. Smallest change: one new table, no change to an existing one. Rows live
--    at most ``stream_wait_seconds`` (deleted when their session ends, and
--    any left by a crashed node pruned by the maintenance sweep once
--    expired) and, by design, hold NO puller: no instance id, key or
--    address — a ticket, the blob, the scope, the readers' sealed hints
--    verbatim, timestamps, an attempt count and the hash of the current
--    claim's random secret (not linkable to any household).
CREATE TABLE IF NOT EXISTS gfs_space_media_member_wants (
    -- The session's ticket (names the node that holds the session).
    ticket         TEXT PRIMARY KEY,
    -- Only listed spaces: a private channel has its own seat-holder path.
    scope_kind     TEXT NOT NULL CHECK(scope_kind IN ('space')),
    scope_id       TEXT NOT NULL,
    blob_id        TEXT NOT NULL,
    mode           TEXT NOT NULL CHECK(mode IN ('stream', 'cache')),
    -- The readers' sealed hints, verbatim (a JSON list, at most 4).
    hints          TEXT NOT NULL,
    -- Unix seconds the session's wait ends; never offered after.
    expires_at     INTEGER NOT NULL,
    -- Until when one member holds the want (one serve per blob).
    claimed_until  INTEGER NOT NULL DEFAULT 0,
    -- sha256 (hex) of the random secret the claiming pull got: only that
    -- puller can serve or decline — it names no one ('' = not claimed).
    claim_hash     TEXT NOT NULL DEFAULT '',
    -- Members that declined it so far (bounded by the session).
    attempts       INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_member_wants_scope
    ON gfs_space_media_member_wants(scope_kind, scope_id, created_at);
CREATE INDEX IF NOT EXISTS idx_gfs_space_media_member_wants_expires
    ON gfs_space_media_member_wants(expires_at);

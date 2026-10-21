-- 0119 — per-moment share links for public moments. Follows 0118
-- (moment public keys).
--
-- Owner decision 2026-10-20: the GFS's public ``/moments/<person>`` page
-- shows the profile and a Follow button only; a moment opens for a stranger
-- only through a link its author shares. That link carries the moment's
-- ITEM KEY in its fragment (never the author's generation key) and opens a
-- sealed share copy on the GFS. The item key is derived from the day's
-- ratchet step, which the author's household overwrites daily (forward
-- secrecy at rest) — so to mint a link later in the moment's life, the
-- household keeps that one key per public moment.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited what already holds a public moment's keys or state:
--       ``moment_public_keys`` (one row per author × generation, the step
--       key advanced IN PLACE — yesterday's item keys can't be recomputed),
--       ``moments`` (federated row shape; a key column there would ride
--       every moment read and every export), and the in-memory blob
--       bookkeeping of ``MomentPublicMediaOutbound`` (lost on restart).
--   (2) Alternatives rejected: deriving item keys from the generation ROOT
--       (keeping the root defeats the daily ratchet for every moment);
--       keeping item keys in memory (a restart would make every live
--       moment unshareable); uploading a share copy for EVERY public moment
--       at publish (4 KB per moment per GFS that nobody may ever open — the
--       GFS would hold a copy of each moment instead of only shared ones).
--   (3) Smallest change: one table keyed by the moment, cascading with it
--       (so it is gone when the moment expires or is deleted); the key
--       KEK-sealed with the moment id as associated data.
CREATE TABLE IF NOT EXISTS moment_public_shares (
    moment_id        TEXT PRIMARY KEY REFERENCES moments(id) ON DELETE CASCADE,
    author_user_id   TEXT NOT NULL,
    -- KEK-encrypted 32-byte item key (``KeyManager.encrypt``, AAD = id).
    item_key_sealed  TEXT NOT NULL,
    -- The moment's ``media`` descriptor per GFS holding its file (JSON
    -- ``{gfs_id: descriptor}``) — a link names a GFS, which must hold it.
    -- NULL / empty without one.
    media_by_gfs     TEXT,
    -- The share copy, once a link was asked for: its blob address and the
    -- GFS it was uploaded to.
    share_blob_id    TEXT,
    share_gfs_id     TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

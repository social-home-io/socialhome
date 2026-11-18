-- 0147 — a rotated space key reaches every GFS that still pins the old one.
--
-- 1. ``gfs_withdrawn_listings`` — one row per (space, GFS connection) whose
--    listing the owner WITHDREW but whose row the GFS keeps: a withdrawal
--    hides the listing from discovery only, the server keeps the space's
--    pinned authority key and keeps relaying to the followers it has. A
--    later key rotation (an admin lost the signing key) must move that pin
--    too, or the revoked key stays valid there for good. ``pinned_key`` is
--    the authority key the server last CONFIRMED pinning (NULL = unknown);
--    the row is "owed a re-pin" while it differs from the space's current
--    key. Deleted when the space is listed there again, when the server
--    answers that it holds no row, or with the connection (FK cascade).
-- 2. ``gfs_channel_withdrawals.channel_seed`` (NULL default) — the opaque
--    channel's signing seed, wrapped under the household KEK. A private
--    space's channel key derives from the space authority seed, so after a
--    rotation the owner could no longer sign the unregister a server still
--    owed (it was down): the row was dropped and the old channel lived on.
--    With the wrapped seed the unregister is retried until confirmed.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every code path touching this data: ``gfs_space_publications``
--       (one row per LIVE listing; deleted by ``unpublish_space`` — read by
--       every listing, relay, heal and withdrawal-retry path, its ``status``
--       CHECKed to listing states), ``spaces.gfs_servers`` (the owner's
--       signed list — replaced in place, the dropped server is gone with
--       it), ``GfsServerSelectionService.retry_withdrawals`` (retries a
--       withdrawal that did NOT land; one that landed leaves nothing),
--       ``SpaceAuthorityRotationService._repin_gfs`` /
--       ``GfsConnectionService.republish_space`` / ``heal_space_pins``
--       (all walk the publication rows only), 0095
--       ``gfs_channel_withdrawals`` and ``GfsChannelService`` (``_retire``,
--       ``withdraw_pending``, ``_unregister_one`` — which re-derives the
--       channel key from the CURRENT seed and gives up when it changed).
--       Nothing records "this server still pins a key for a space we no
--       longer list there", and nothing keeps a retired channel's key.
--   (2) Non-migration alternatives considered and rejected:
--       * Keeping the publication row with a new ``withdrawn`` status — the
--         CHECK forbids it and every listing / relay / heal path would have
--         to learn to skip it (same finding as 0095).
--       * Re-pinning on every connected GFS at rotation — tells servers that
--         never listed the space that it exists.
--       * RAM only — a restart forgets which servers the space was withdrawn
--         from; unlike a live listing it cannot be re-derived from any row.
--         (Which LIVE listings confirmed the current key IS kept in RAM
--         only: every reconnect re-publishes them anyway.)
--       * Keeping the retired AUTHORITY seed to re-derive the channel key —
--         stores a strictly more powerful secret than the one needed.
--       * An ``instance_config`` blob per retired channel — a second place
--         to clean up; the withdrawal row already has the right lifetime
--         (deleted on confirm, cascaded with the connection).
--   (3) Smallest change: one new, empty table and one NULL-default column
--       (metadata-only, no rewrite, no backfill). No existing row changes.

CREATE TABLE IF NOT EXISTS gfs_withdrawn_listings (
    space_id          TEXT NOT NULL,
    gfs_connection_id TEXT NOT NULL
                      REFERENCES gfs_connections(id) ON DELETE CASCADE,
    pinned_key        TEXT
                      CHECK (pinned_key IS NULL
                             OR (length(pinned_key) = 64
                                 AND pinned_key NOT GLOB '*[^0-9a-f]*')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, gfs_connection_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_withdrawn_listings_conn
    ON gfs_withdrawn_listings(gfs_connection_id);

ALTER TABLE gfs_channel_withdrawals ADD COLUMN channel_seed TEXT;

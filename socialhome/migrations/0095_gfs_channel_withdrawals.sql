-- 0095 — ``gfs_channel_withdrawals``: the owner's pending withdrawals of a
-- private space's opaque channel from connection servers it no longer runs
-- on. Follows 0094 (proof-of-possession ratchet).
--
-- A private space with ``private_gfs`` on runs its channel on the GFSs its
-- owner chose (the signed ``spaces.gfs_servers`` list). When the choice
-- shrinks — a server dropped, or the option turned off — the owner must
-- unregister the channel there (anonymous, channel-key signed). That request
-- can fail (server down, connection suspended) or the household can stop
-- between replacing the list and sending it. One row per (space, connection,
-- channel) still to withdraw, written BEFORE the new list is stored and
-- deleted once the server confirmed (or can never confirm: the seed changed).
-- Retried on every reconnect of that connection (``GfsChannelService.heal``)
-- and on every later change of the space's servers.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every code path touching the channel's servers: the owner's
--       signed list (``spaces.gfs_servers``, 0093 — replaced in place, the
--       old entries are gone with it), the stored channel
--       (``spaces.gfs_channel_id`` / ``_pk`` / ``_healed_at``, one current
--       channel per space), ``gfs_space_publications`` (listings of PUBLIC
--       spaces, ``status`` CHECKed to listing states and read by every
--       listing / relay path), the in-memory registration set of
--       ``GfsChannelService`` (lost on restart), member-side grants
--       (``space_keys.gfs_channel``, the member's copy). None records "this
--       server still holds a channel the owner must take down".
--   (2) Non-migration alternatives considered and rejected:
--       * RAM only (the first revision): a restart or crash between storing
--         the shrunk list and the unregister loses the withdrawal for good.
--       * A ``gfs_space_publications`` row with a new status — the CHECK
--         forbids it, and every listing, relay and withdrawal path would
--         have to learn to skip it.
--       * Keeping the previous signed list next to the current one — a
--         second ``spaces`` column, and still no per-server "confirmed".
--       * Unregistering on every reconnect of any server — would hand the
--         opaque channel id to servers the owner never chose.
--       * Starting a fresh channel on every shrink — the dropped server keeps
--         the old channel (and its seats) until its idle sweep.
--   (3) Smallest change: one new, empty table; no existing row changes. The
--       connection FK cascades, so a disconnected GFS leaves no orphan
--       (``GfsChannelService.on_gfs_disconnect`` tries the unregister first,
--       in the background, from the connection it was handed).

CREATE TABLE IF NOT EXISTS gfs_channel_withdrawals (
    space_id          TEXT NOT NULL,
    gfs_connection_id TEXT NOT NULL
                      REFERENCES gfs_connections(id) ON DELETE CASCADE,
    channel_id        TEXT NOT NULL
                      CHECK (length(channel_id) = 32
                             AND channel_id NOT GLOB '*[^0-9a-f]*'),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, gfs_connection_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_channel_withdrawals_conn
    ON gfs_channel_withdrawals(gfs_connection_id);

-- 0090 — which connection servers hold a subscriber seat of THIS household,
-- per space. A seat is taken by a signed ``POST /gfs/subscribe`` — by a
-- follower subscribing to a GFS-mirrored space
-- (``GfsSpaceMirrorService.subscribe_to_gfs``) or by a member's auto-subscribe
-- on every capable server listing a space it writes in
-- (``GfsMemberPublishService.ensure_subscribed``). The (un)subscribe is
-- identity-bound, so it may only ever go to a server that seats us: sending
-- it anywhere else tells that operator this household follows the space.
--
-- One row per (space, server). The server is keyed by ``gfs_instance_id`` —
-- the GFS's own identity — not by ``gfs_connections.id``, which is a local
-- uuid re-minted on every disconnect + re-pair while the server keeps our
-- seat. Deliberately NO foreign keys: a seat outlives a re-pair (no FK to
-- ``gfs_connections``) and outlives a purged mirror row (no FK to
-- ``spaces``) until it is torn down on the server — a cascade would forget
-- the one fact that lets the household send that unsubscribe.
--
-- Backfill: a v_44+ mirror recorded the seating connection in
-- ``spaces.mirror_gfs_id``; where that connection still exists and a local
-- ``subscriber`` seat is held, the seat is recorded under its server id.
-- Pre-v44 mirrors (NULL provenance) and those whose connection is gone are
-- not knowable here; the household tears those down reactively (a relay
-- frame for a space it holds no seat in → unsubscribe from that server).
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data. ``grep -rn
--       "subscribe_to_gfs_space\|unsubscribe_from_gfs_space\|mirror_gfs_id"
--       socialhome``: the subscribe is sent from the mirror
--       (``subscribe_to_gfs`` on first subscribe, ``resubscribe_all`` on
--       every GFS-WS reconnect) and from member publish (``_subscribe``);
--       the unsubscribe only from ``GfsSpaceMirrorService.unsubscribe``.
--       ``spaces.mirror_gfs_id`` (0066) records the ONE connection that
--       seated a follower mirror, by local uuid, for the v_44 pin heal.
--       ``gfs_space_publications`` is the owner's publish record, keyed by
--       connection uuid and cascaded away on unpair. ``public_space_cache``
--       is the directory poll's cache — no server id, purged by TTL.
--   (2) Non-migration alternatives considered and rejected:
--       * ``spaces.mirror_gfs_id`` — one value per space, so it cannot hold
--         the several servers a member auto-subscribes on; a local uuid, so
--         it dangles after a re-pair (the seat is then never re-taken nor
--         torn down); and rewriting it to the server id would also move the
--         pin-heal trust anchor, a separate reviewed change.
--       * Deriving seats from the servers' directories at teardown — a
--         space withdrawn from a directory keeps its relay and its
--         subscribers on the GFS, so the server that seats us would be the
--         one we never contact; and a server listing a space we never
--         subscribed on would receive an identity-bound unsubscribe.
--       * In-memory bookkeeping — lost on restart, which is exactly when
--         the reconnect self-heal runs.
--   (3) Smallest change: one additive table + one index, no rewrite of any
--       existing row; the backfill only inserts.

CREATE TABLE IF NOT EXISTS gfs_space_seats (
    space_id        TEXT NOT NULL,
    gfs_instance_id TEXT NOT NULL,
    seated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (space_id, gfs_instance_id)
);

-- The reconnect self-heal reads every seat on ONE server.
CREATE INDEX IF NOT EXISTS idx_gfs_space_seats_gfs
    ON gfs_space_seats(gfs_instance_id);

INSERT OR IGNORE INTO gfs_space_seats(space_id, gfs_instance_id)
SELECT s.id, gc.gfs_instance_id
  FROM spaces s
  JOIN gfs_connections gc ON gc.id = s.mirror_gfs_id
 WHERE EXISTS (
        SELECT 1 FROM space_members m
          JOIN users u ON u.user_id = m.user_id
         WHERE m.space_id = s.id AND m.role = 'subscriber'
       );

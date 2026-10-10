-- 0092 — which connection servers hold a subscriber seat of THIS household,
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
-- ``gfs_connection_id`` / ``gfs_public_key`` / ``gfs_inbox_url`` (nullable)
-- bind the seat to the local connection it was taken over, the server key
-- pinned on it and the server's URL. A connection is this seat's server
-- only when id, key AND (normalized) URL all match: pairing learns the id
-- and key from an unauthenticated ``/gfs/info``, so an impostor can serve
-- both, but not answer at the real server's address. Every seat read
-- (re-subscribe, teardown, reactive teardown) and the v_44 pin-heal anchor
-- move (``spaces.mirror_gfs_id`` follows a re-pair only to a connection
-- matching all three) apply that check. They are needed once the old
-- ``gfs_connections`` row is deleted, which every disconnect does.
--
-- An unpair keeps the rows and marks them ``detached`` (``detached_at``:
-- when, UTC, stamped only while the clock looks sane), then sends the
-- server's unsubscribes in the background — never inside the unpair
-- request; a confirmed one sets ``released``. A re-pair with the same id,
-- key and address re-takes the detached seats a local user still wants
-- and releases the rest; the v_44 pin anchor follows through the same
-- rows. A local sweep (startup + every reconnect, no request) drops a
-- detached row only once it is ``released`` and 90 days old, confirmed on
-- a second sweep at least a day later (``expiry_seen_at``) so one clock
-- jump never drops it; an unreleased row is an unsubscribe-only tombstone
-- kept until a matching connection takes the unsubscribe. A server whose
-- id and key return at another address is NOT re-bound (only a proof of
-- possession, planned in a follow-up, can tell a move from an impostor):
-- such seats keep their rows as tombstones and are logged once
-- (``refollow_warned``) as needing a re-follow — as are seats whose server
-- id and address return under a different key. A row a local user still
-- wants is never aged out (it is what a re-pair re-takes), and a pending
-- tombstone is retried by the sweep whenever a matching connection is
-- active.
--
-- Backfill: a v_44+ mirror recorded the seating connection in
-- ``spaces.mirror_gfs_id``; where that connection still exists and a local
-- ``subscriber`` seat is held, the seat is recorded under its server id.
-- Pre-v44 mirrors (NULL provenance) and those whose connection is gone are
-- not knowable here. A pre-v44 mirror still listed somewhere has its seat
-- recorded by its first reconnect re-subscribe; one whose space was
-- withdrawn from every listing before the upgrade keeps its seat on the one
-- GFS that already holds it until that GFS drops it (accepted residual,
-- owner decision) — no other GFS learns anything.
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
--       * Rewriting ``mirror_gfs_id`` to the server id — moves the pin-heal
--         trust anchor from "this pairing" to "any pairing of that id",
--         which a re-pair under a different key must not inherit; the
--         stored key keeps that check possible after the old row is gone.
--   (3) Smallest change: one additive table + one index, no rewrite of any
--       existing row; the backfill only inserts.

CREATE TABLE IF NOT EXISTS gfs_space_seats (
    space_id        TEXT NOT NULL,
    gfs_instance_id TEXT NOT NULL,
    seated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    gfs_connection_id TEXT,
    gfs_public_key    TEXT,
    gfs_inbox_url     TEXT,
    detached          INTEGER NOT NULL DEFAULT 0 CHECK (detached IN (0, 1)),
    detached_at       TEXT,
    released          INTEGER NOT NULL DEFAULT 0 CHECK (released IN (0, 1)),
    expiry_seen_at    TEXT,
    refollow_warned   INTEGER NOT NULL DEFAULT 0 CHECK (refollow_warned IN (0, 1)),
    PRIMARY KEY (space_id, gfs_instance_id),
    -- Only an unpaired (detached) seat can have been released / stamped,
    -- and only a released one is ever on its way out.
    CHECK (released = 0 OR detached = 1),
    CHECK (detached_at IS NULL OR detached = 1),
    CHECK (expiry_seen_at IS NULL OR released = 1)
);

-- The reconnect self-heal reads every seat on ONE server.
CREATE INDEX IF NOT EXISTS idx_gfs_space_seats_gfs
    ON gfs_space_seats(gfs_instance_id);

INSERT OR IGNORE INTO gfs_space_seats(
    space_id, gfs_instance_id, gfs_connection_id, gfs_public_key, gfs_inbox_url
)
SELECT s.id, gc.gfs_instance_id, gc.id, gc.public_key, gc.inbox_url
  FROM spaces s
  JOIN gfs_connections gc ON gc.id = s.mirror_gfs_id
 WHERE EXISTS (
        SELECT 1 FROM space_members m
          JOIN users u ON u.user_id = m.user_id
         WHERE m.space_id = s.id AND m.role = 'subscriber'
       );

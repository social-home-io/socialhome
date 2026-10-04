-- 0016 — opaque channels for PRIVATE spaces (v_51): member publishing for a
-- private space whose members include households seated through an invite
-- link, without this server ever learning which space it is.
--
-- A channel is a random 128-bit id plus a channel public key (HKDF-derived
-- from the space authority seed on the households, unlinkable to the space
-- key), registered anonymously by a seed holder
-- (``socialhome/global_server/channels.py``). This server needs to know, per
-- channel: the pinned key, the content epoch (so a writer removed at the last
-- rotation stops being relayed), the publish mode and the strict-mode writer
-- key pins; and which registered households hold a fan-out seat.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every path that touches this data. Spaces live in
--       ``global_spaces`` (0001) — the public directory, the /spaces/{id}
--       pages, invite links, the cluster sync, reports and the admin console
--       all read it; its ``owning_instance`` is NOT NULL and a foreign key to
--       ``client_instances``. Subscriptions live in ``space_subscribers``
--       (0001, FK to ``global_spaces``). The epoch tiers and writer key pins
--       of 0014 / 0015 are columns on ``global_spaces``. Offline delivery is
--       ``gfs_envelope_queue`` (0011 / 0014, ``frame_type = 'relay'``), which
--       stores the identity-free frame opaquely and needs no change: a
--       channel frame is just another relay frame.
--   (2) Alternatives considered and rejected. Reusing ``global_spaces`` with
--       a kind column: every row there NAMES ITS OWNER (``owning_instance``
--       NOT NULL + FK) — the very fact a channel must withhold — and every
--       directory, page, sync and admin query would need a kind filter, so
--       one forgotten filter lists a private channel publicly. Reusing
--       ``space_subscribers``: its FK points at ``global_spaces``. Keeping
--       channels in memory: a restart would forget pins (letting anyone
--       re-register a channel under their own key) and epochs (reopening the
--       relay for a removed writer). Keyed by the space id: the whole point
--       is that this server never sees it. So: two new tables keyed by the
--       opaque id; the queue, the fan-out workers, the epoch rules and the
--       pin rules are reused unchanged in code.
--   (3) Smallest possible change: two additive ``CREATE TABLE``s and one
--       index; no existing row is touched. Every epoch / pin column is
--       NULL-defaulted (NULL = nothing learned yet); the mode defaults to
--       ``'trusted'`` with the same CHECK as 0015.
--
-- What the rows reveal is what the traffic already shows: that some channel
-- exists, its key, when its epoch moves, its mode, and which registered
-- households subscribe (the accepted residual, signed off in
-- docs/principles.md). Nothing names a space, a space key or an owner:
-- registration and notices are anonymous (channel-key-signed), and no
-- column holds a household id except the subscriber seats. The maintenance
-- loop sweeps channels idle for 30 days, and within a day those that never
-- got a notice or a seat; registration stops at a server-wide row cap.

CREATE TABLE IF NOT EXISTS gfs_channels (
    channel_id              TEXT PRIMARY KEY
                            CHECK (length(channel_id) = 32
                                   AND channel_id NOT GLOB '*[^0-9a-f]*'),
    -- The pinned channel key (b64url) and its suite — immutable: there is
    -- no re-pin (a household starts a fresh channel instead). No CHECK on the suite:
    -- a Phase-2 PQ suite must land without a migration; the code validates.
    channel_suite           TEXT NOT NULL,
    channel_pk              TEXT NOT NULL,
    registered_at           INTEGER NOT NULL,
    -- Last registration / notice / publish (unix seconds) — the idle sweep.
    last_active_at          INTEGER NOT NULL,
    -- Single-tier content epoch: ``content_epoch`` and the one before it
    -- (open for the grace after ``content_epoch_raised_at``).
    content_epoch           INTEGER,
    content_epoch_prev      INTEGER,
    content_epoch_raised_at INTEGER,
    publish_mode            TEXT NOT NULL DEFAULT 'trusted'
                            CHECK (publish_mode IN ('trusted', 'strict')),
    publish_mode_at         INTEGER,
    writer_key_epoch        INTEGER,
    writer_key_pk           TEXT,
    writer_key_prev_epoch   INTEGER,
    writer_key_prev_pk      TEXT
);

-- A member household's fan-out seat, with the epoch of the pass it proved
-- membership with: a seat whose pass epoch is no longer open receives
-- nothing (a household removed at a rotation drops out after the grace).
CREATE TABLE IF NOT EXISTS gfs_channel_subscribers (
    channel_id  TEXT NOT NULL REFERENCES gfs_channels(channel_id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    pass_epoch  INTEGER NOT NULL,
    joined_at   INTEGER NOT NULL,
    PRIMARY KEY (channel_id, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_channel_subscribers_instance
    ON gfs_channel_subscribers(instance_id);

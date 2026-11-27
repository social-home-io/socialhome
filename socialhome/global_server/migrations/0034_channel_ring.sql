-- GFS 0034 — the channel ring (v_97, GFS capability ``channel_ring``).
-- Follows 0033.
--
-- Owner decisions 2026-11-27 (design channel-items, F1–F8): every channel
-- keeps ONE ring of recent items (64 MiB / 32 768 items / 24 h durable,
-- 15 min ephemeral, 8 GiB per server). Households watching a channel get
-- items live; everyone else gets a head ping every 15 min and pulls what
-- it missed with ``channel_fetch``. The ring replaces the per-seat offline
-- copy of a channel item for a household that opted in (``channel_ring``
-- in its WS hello).
--
-- Audit (CLAUDE.md "Before adding a SQL migration"):
--
-- 1. Code paths touching this data: ``channels.GfsChannelService``
--    (register / unregister / epoch / subscribe / publish-anon and the
--    cluster audience resolver), ``repositories.SqliteGfsChannelRepo``
--    (every ``gfs_channels`` / ``gfs_channel_subscribers`` read and write,
--    the idle / unused sweeps that delete channel rows), the offline relay
--    queue (``envelope_relay.queue_relay_many`` / ``fan_out_relay``, which
--    keyed copies per recipient in ``relay_queue``), the WS hello
--    (``routes/ws.py``, ``client_instances.relay_seen_at``), the admin
--    overview, and the maintenance loop.
-- 2. Non-migration alternatives rejected: the existing per-recipient
--    ``relay_queue`` holds one copy per OFFLINE seat for 24 h — 10 000
--    follower seats are 10 000 copies of every item, and it has no order a
--    household can resume from; reusing it as the ring would keep exactly
--    that cost. Keeping the ring in memory loses it on every restart and
--    splits it across cluster nodes (each node a different seq): a cursor
--    would only work on one node. The sequence number must be gap-free
--    across nodes, which needs the shared database. The ``channel_ring``
--    opt-in must outlive the socket (an offline household gets no offline
--    copy), so it cannot live only in the in-memory socket registry.
-- 3. Smallest change: one new table (dropped with its channel: ON DELETE
--    CASCADE, so unregister and the idle sweeps empty it), three additive
--    columns on ``gfs_channels`` with defaults (``head_seq`` 0,
--    ``ring_bytes`` 0, ``ring_items`` 0, ``durable_floor_seq`` 0, ``ring_id`` NULL until the
--    first item) and a partial index for the server cap, one NOT NULL
--    DEFAULT 0 column on ``client_instances``. No backfill, no rewrite.
--
-- What it reveals: what the traffic already shows — that a channel had an
-- item of some size bucket and ring class at some time. No space, no
-- household (the ring is per channel, never per seat), no item type.
-- ``ring_id`` (64 random bits) names one life of a channel's ring: a fresh
-- registration starts a new one, so a cursor from an earlier life reads as
-- a gap.

ALTER TABLE gfs_channels ADD COLUMN head_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE gfs_channels ADD COLUMN ring_id TEXT;
ALTER TABLE gfs_channels ADD COLUMN ring_bytes INTEGER NOT NULL DEFAULT 0;
-- Rows the ring holds (its count bound is on rows, not on seq distance:
-- expired location pins never push durable items out).
ALTER TABLE gfs_channels ADD COLUMN ring_items INTEGER NOT NULL DEFAULT 0;
-- The highest seq of a DURABLE item the ring dropped (evicted or expired):
-- a cursor below it missed something that is gone (a gap); an expired
-- ephemeral item (a location pin) never makes a gap.
ALTER TABLE gfs_channels ADD COLUMN durable_floor_seq INTEGER NOT NULL DEFAULT 0;
-- The server cap trims the biggest rings first.
CREATE INDEX IF NOT EXISTS idx_gfs_channels_ring_bytes
    ON gfs_channels(ring_bytes) WHERE ring_bytes > 0;

CREATE TABLE IF NOT EXISTS gfs_channel_ring (
    channel_id  TEXT NOT NULL REFERENCES gfs_channels(channel_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    epoch       INTEGER NOT NULL,
    ring_class  TEXT NOT NULL CHECK (ring_class IN ('durable', 'ephemeral')),
    -- The item's ciphertext exactly as published (base64url text).
    payload     TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (channel_id, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_gfs_channel_ring_created
    ON gfs_channel_ring(created_at);

-- The household said ``channel_ring`` in a WS hello: it reads its channels
-- from the ring and gets no offline copy of a channel item (except the
-- ``push_all`` items every seat gets). Cleared again by a hello without it.
ALTER TABLE client_instances ADD COLUMN channel_ring INTEGER NOT NULL DEFAULT 0;

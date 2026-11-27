-- 0158 — ``space_item_outbox``: the durable retry queue for a GFS space's
-- content items (v_97, owner 2026-11-27: "a DURABLE RETRY queue, NOT a
-- fallback to the legacy per-household fan-out").
--
-- One row per (item, planned GFS): the item's ciphertext (already sealed
-- under the space content key — never plaintext), the epoch it was sealed
-- at, its ring class, and the retry state for that one GFS. A row goes when
-- that GFS accepted the item; a transient answer (down, 5xx, 429) moves
-- ``next_attempt_at`` (a 429's ``Retry-After`` honoured); a permanent one
-- marks it ``failed`` and the author sees "Couldn't send to {space}" with
-- Retry / Discard. ``gfs_id = ''`` is an item whose GFS list was empty
-- when it was sealed (no grant yet): it is planned when it is due. A row
-- without ``payload`` is a failure that never stored bytes (too large, the
-- space's queue full, sealing failed): Discard only. ``supersede_key``
-- names latest-only state (a person's location pin): a newer one replaces a
-- row still waiting. Local only; nothing here goes on a wire.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that publishes a channel item today:
--       ``GfsMemberPublishService.seal_event_item`` → ``SenderBudget`` →
--       ``GfsChannelService.publish_sealed`` → ``_first_attempt`` →
--       ``GfsPublishRetryQueue`` (in memory, 64 per connection, ~13 min,
--       lost on restart or when full), and ``FederationService
--       ._broadcast_as_item`` (the legacy fallback the owner ruled out).
--       Nothing persisted holds a GFS-space item between the seal and the
--       GFS's answer.
--   (2) Alternatives rejected: ``federation_outbox`` — keyed by a recipient
--       household, it stores a signed per-peer envelope and its redelivery
--       re-signs it with our identity; a channel publish has no recipient
--       household and must carry no household signature (anonymous, with
--       the channel writer key), so it would need a row kind every outbox
--       path (cap, supersede, retention, unpair purge, the payload
--       triggers of 0145) must learn to skip; ``instance_config`` — one
--       JSON blob rewritten per item, no per-GFS backoff; the in-memory
--       queue — lost on restart, which is what the owner asked to fix.
--   (3) Smallest change: one additive table with its primary key and one
--       due-index; no change to any existing table or row.

CREATE TABLE IF NOT EXISTS space_item_outbox (
    item_id         TEXT NOT NULL,
    gfs_id          TEXT NOT NULL,
    space_id        TEXT NOT NULL,
    epoch           INTEGER NOT NULL,
    ring_class      TEXT NOT NULL
                    CHECK (ring_class IN ('durable', 'ephemeral')),
    event_type      TEXT NOT NULL,
    author_user_id  TEXT NOT NULL DEFAULT '',
    supersede_key   TEXT,
    payload         TEXT,
    bytes           INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'failed')),
    error           TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (item_id, gfs_id),
    CHECK (status = 'failed' OR payload IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_space_item_outbox_due
    ON space_item_outbox(status, next_attempt_at);

CREATE INDEX IF NOT EXISTS idx_space_item_outbox_space
    ON space_item_outbox(space_id, status);

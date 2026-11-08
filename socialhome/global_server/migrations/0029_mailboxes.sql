-- GFS 0029 — anonymous per-space mailboxes (GFS redesign PR 1). Follows 0028.
--
-- A household reached through this server gets one mailbox per (household,
-- space, generation): an address derived from a mailbox signing key
-- (``mailbox_id = base32(SHA-256("sh-mbx-v1" ‖ sign_pk))[:32]``), registered
-- anonymously with proof of possession (``POST /gfs/mailbox/register``) and
-- claimed on its own anonymous WebSocket (``GET /gfs/mailbox/ws``). Envelopes
-- and binary media frames addressed to it wait in the EXISTING
-- ``gfs_envelope_queue`` / ``gfs_media_queue`` rows keyed by the mailbox id —
-- it has the instance-id shape on purpose.
--
-- What this table holds is only what the server needs to authorize a claim:
-- the pinned key (trust on first use) and two timestamps for the idle
-- sweep. NO household identity, NO client address, NO space id — the server
-- cannot tell whose mailbox it is or which mailboxes belong together.
--
-- Migration audit (mandatory 3 points, CLAUDE.md). This migration does
-- three things: (a) ``gfs_mailboxes``; (b) a ``pool`` column on both relay
-- queues, the ``gfs_queue_totals`` running totals (backfilled, kept by four
-- triggers) and an index for the pool expiry sweep; (c) ``gfs_known_gfs_ids``.
--
--   (1) Audited every code path that already touches this data.
--       (a) ``client_instances`` is the only key table a relay recipient is
--       checked against (``GfsEnvelopeRelay.accept``); ``gfs_moment_key_mailbox``
--       (0021) is per (author, follower HOUSEHOLD); channel seats (0016) are
--       per space and bound to instances — nothing stores a recipient that
--       is not an instance. (b) The two queues are written and read only by
--       ``SqliteGfsEnvelopeQueueRepo`` (enqueue / enqueue_relay /
--       enqueue_media / delete / delete_media / prune_* / list_* /
--       next_media_for) and ``SqliteGfsMailboxRepo.prune_idle``; every
--       insert and delete runs through the triggers. (c) The GFS ids a
--       registration / claim may be bound to were only the node's own id,
--       its ``instance_id_aliases`` (TOML) and its live siblings' ids (in
--       memory, ``ClusterService``).
--
--   (2) Non-migration alternatives considered and REJECTED:
--       (a) a ``client_instances`` row per mailbox (that is the household
--       directory; a mailbox must never be listed or treated as one); a
--       per-space seat (would tell the server which space a mailbox is
--       in); memory only (a restart or another node would lose the TOFU
--       pin); deriving the key at claim time (the id is a hash of the key —
--       the claimant would send the key and the id would bind nothing).
--       (b) a per-insert ``SUM`` over the pool (a full scan inside the write
--       lock on every queued envelope); a counter kept in code (every
--       delete path would have to remember it — the triggers can't be
--       forgotten); deciding the pool by joining ``gfs_mailboxes`` at read
--       time (a subquery per row, and a swept mailbox would move its rows
--       between pools). (c) ``instance_id_aliases`` alone: an operator lists
--       former ids it retired on purpose, but a cluster on per-alloc ids
--       (``gfs-N``) has ids nobody lists, and a live sibling's id is gone
--       from memory the moment that alloc is; households pinned to it could
--       never claim again.
--
--   (3) Smallest possible change: two new tables of a few rows / one row
--       per mailbox, one 4-row counter table, two ADD COLUMNs with a
--       NOT NULL default (no existing row rewritten — every existing row is
--       a household's), indexes, one backfill INSERT per counter row and
--       four triggers. Nothing renamed or dropped.
--
-- Value domain:
--   mailbox_id     32 lowercase base32 chars, recomputed by the server from
--                  ``sign_pk`` before anything is stored.
--   sign_pk        the 32-byte Ed25519 mailbox public key (pinned on first
--                  use; a claim must be signed by it).
--   created_at     unix seconds of the first registration.
--   last_claim_at  unix seconds of the latest successful claim, NULL until
--                  the first one. The idle sweep drops a mailbox whose
--                  COALESCE(last_claim_at, created_at) is older than
--                  ``mailbox_idle_days``.

CREATE TABLE IF NOT EXISTS gfs_mailboxes (
    mailbox_id    TEXT    PRIMARY KEY,
    sign_pk       BLOB    NOT NULL CHECK (length(sign_pk) = 32),
    created_at    INTEGER NOT NULL,
    last_claim_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_gfs_mailboxes_idle
    ON gfs_mailboxes(COALESCE(last_claim_at, created_at));

-- ``count_created_since`` (the server-wide daily cap, checked inside the
-- insert transaction) reads by ``created_at``.
CREATE INDEX IF NOT EXISTS idx_gfs_mailboxes_created
    ON gfs_mailboxes(created_at);

-- ── Queue pools with running totals ───────────────────────────────────────
--
-- Mailbox ids are free to mint, so their queued frames live in a POOL of
-- their own with its own server-wide byte ceiling: they never count
-- against, and never evict, a household's. ``pool`` says which pool a row
-- was queued in (decided by the relay at insert time); existing rows are
-- households' (the default — no row is rewritten).
--
-- The ceilings are checked on every insert inside the writer transaction;
-- summing the pool's rows there would be a full scan under the write lock.
-- ``gfs_queue_totals`` keeps the running byte total per (queue, pool),
-- maintained by triggers in the same statement as every insert and delete
-- — so no code path (drain, sweeps, idle sweep) can let it drift (and the
-- hourly maintenance recomputes them anyway).
-- (0014 chose a covering index over counter rows for the RELAY caps; the
-- pool totals here are a single global number per pool, which an index
-- cannot answer without reading every row.)
--
ALTER TABLE gfs_envelope_queue ADD COLUMN pool TEXT NOT NULL DEFAULT 'household'
    CHECK (pool IN ('household', 'mailbox'));
ALTER TABLE gfs_media_queue ADD COLUMN pool TEXT NOT NULL DEFAULT 'household'
    CHECK (pool IN ('household', 'mailbox'));

-- The expiry sweep a full pool runs before refusing (an index range, not a
-- scan): expired rows never hold room in a pool.
CREATE INDEX IF NOT EXISTS idx_gfs_envelope_queue_pool_expiry
    ON gfs_envelope_queue(pool, expires_at);

CREATE TABLE IF NOT EXISTS gfs_queue_totals (
    queue TEXT    NOT NULL CHECK (queue IN ('envelope', 'media')),
    pool  TEXT    NOT NULL CHECK (pool IN ('household', 'mailbox')),
    bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (queue, pool)
);

INSERT OR IGNORE INTO gfs_queue_totals(queue, pool, bytes)
SELECT 'envelope', 'household',
       COALESCE(SUM(CASE WHEN size_bytes > 0 THEN size_bytes
                         ELSE LENGTH(sealed_json) END), 0)
  FROM gfs_envelope_queue WHERE frame_type = 'envelope';
INSERT OR IGNORE INTO gfs_queue_totals(queue, pool, bytes)
    VALUES ('envelope', 'mailbox', 0);
INSERT OR IGNORE INTO gfs_queue_totals(queue, pool, bytes)
SELECT 'media', 'household', COALESCE(SUM(size_bytes), 0) FROM gfs_media_queue;
INSERT OR IGNORE INTO gfs_queue_totals(queue, pool, bytes)
    VALUES ('media', 'mailbox', 0);

-- Envelope rows only (``frame_type = 'envelope'``): member relay items have
-- their own cap (``enqueue_relay``).
CREATE TRIGGER IF NOT EXISTS trg_gfs_envelope_queue_total_ins
AFTER INSERT ON gfs_envelope_queue WHEN NEW.frame_type = 'envelope'
BEGIN
    UPDATE gfs_queue_totals
       SET bytes = bytes + CASE WHEN NEW.size_bytes > 0 THEN NEW.size_bytes
                                ELSE LENGTH(NEW.sealed_json) END
     WHERE queue = 'envelope' AND pool = NEW.pool;
END;

CREATE TRIGGER IF NOT EXISTS trg_gfs_envelope_queue_total_del
AFTER DELETE ON gfs_envelope_queue WHEN OLD.frame_type = 'envelope'
BEGIN
    UPDATE gfs_queue_totals
       SET bytes = MAX(0, bytes - CASE WHEN OLD.size_bytes > 0 THEN OLD.size_bytes
                                       ELSE LENGTH(OLD.sealed_json) END)
     WHERE queue = 'envelope' AND pool = OLD.pool;
END;

CREATE TRIGGER IF NOT EXISTS trg_gfs_media_queue_total_ins
AFTER INSERT ON gfs_media_queue
BEGIN
    UPDATE gfs_queue_totals SET bytes = bytes + NEW.size_bytes
     WHERE queue = 'media' AND pool = NEW.pool;
END;

CREATE TRIGGER IF NOT EXISTS trg_gfs_media_queue_total_del
AFTER DELETE ON gfs_media_queue
BEGIN
    UPDATE gfs_queue_totals SET bytes = MAX(0, bytes - OLD.size_bytes)
     WHERE queue = 'media' AND pool = OLD.pool;
END;

-- ── GFS ids this cluster has served ───────────────────────────────────────
--
-- A household's mailbox registration and claim are bound to the GFS id it
-- pinned. Every node accepts its own id, its ``instance_id_aliases`` and the
-- ids its shared-key siblings report — but a sibling's id was only known
-- while that sibling was alive, so a household pinned to a retired alloc's
-- id could not claim. Every node writes its own id and the ids of siblings
-- verified under OUR identity key here (day granularity); every node
-- accepts the ids seen within ``KNOWN_GFS_ID_TTL_DAYS`` (90). Holds only this
-- server's own public ids — never anything about a household.
CREATE TABLE IF NOT EXISTS gfs_known_gfs_ids (
    gfs_instance_id TEXT    PRIMARY KEY,
    last_seen_day   INTEGER NOT NULL
);

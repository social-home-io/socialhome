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
-- Migration audit (mandatory 3 points, CLAUDE.md):
--
--   (1) Audited every code path that already touches this data.
--       ``client_instances`` (instance ids + identity / key-wrap keys of
--       registered households) is the only key table a relay recipient is
--       checked against today (``GfsEnvelopeRelay.accept``). The two queues
--       (0011/0014, 0028) hold opaque frames keyed by a 32-char recipient id
--       with no FK, so they take a mailbox id as is. ``gfs_moment_key_mailbox``
--       (0021) is a per-(author, follower HOUSEHOLD) store of sealed keys —
--       identity-bound by design. ``gfs_channels`` / channel seats (0016) are
--       per-space and bind seats to registered instances. Nothing stores a
--       recipient address that is not an instance.
--
--   (2) Non-migration alternatives considered and REJECTED:
--       * a row in ``client_instances`` per mailbox — that table is the
--         household directory (display name, identity key, key-wrap key,
--         status, relay-seen); a mailbox has none of those and must never
--         be listed, counted or treated as a household;
--       * a ``gfs_channels``-style per-space seat — it would tell the server
--         which space a mailbox belongs to, exactly what the redesign hides;
--       * keeping registrations in memory — a restart, or another cluster
--         node taking the claim socket, would lose the pinned key and let
--         anyone re-register (TOFU needs the pin to survive) or refuse the
--         owner's claim; the cluster nodes share this DB for that reason;
--       * deriving the key from the id at claim time — impossible (the id is
--         a hash of the key); the claimant would have to send the key, and
--         then anyone could "claim" by sending their own key for an id that
--         is merely a hash of it, i.e. the id would bind nothing.
--
--   (3) Smallest possible change: ONE additive CREATE TABLE with its
--       primary key and one index for the idle sweep. No existing table is
--       altered, no row rewritten, no backfill. The queues keep their shape;
--       retention stays in code (``mailbox_idle_days`` swept by
--       ``GfsMaintenanceScheduler``).
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
-- — so no code path (drain, sweep, eviction, idle sweep) can let it drift.
-- (0014 chose a covering index over counter rows for the RELAY caps; the
-- pool totals here are a single global number per pool, which an index
-- cannot answer without reading every row.)
--
-- Audit (extends the one above): the two queues are touched by
-- ``SqliteGfsEnvelopeQueueRepo`` only (enqueue / enqueue_relay /
-- enqueue_media / delete / delete_media / prune_* / list_*) and by
-- ``SqliteGfsMailboxRepo.prune_idle``; the triggers cover all of them.
-- Alternatives rejected: per-insert SUM (full scan under the write lock);
-- a counter kept in code (every delete path would have to remember it).
-- Smallest change: two additive NOT NULL DEFAULT columns, one 4-row table,
-- four triggers; existing rows are counted once by the backfill below.

ALTER TABLE gfs_envelope_queue ADD COLUMN pool TEXT NOT NULL DEFAULT 'household'
    CHECK (pool IN ('household', 'mailbox'));
ALTER TABLE gfs_media_queue ADD COLUMN pool TEXT NOT NULL DEFAULT 'household'
    CHECK (pool IN ('household', 'mailbox'));

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
-- accepts the ids seen within ``KNOWN_GFS_ID_TTL_DAYS``. Holds only this
-- server's own public ids — never anything about a household.
CREATE TABLE IF NOT EXISTS gfs_known_gfs_ids (
    gfs_instance_id TEXT    PRIMARY KEY,
    last_seen_day   INTEGER NOT NULL
);

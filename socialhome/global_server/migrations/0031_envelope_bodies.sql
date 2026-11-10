-- GFS 0031 — multi-recipient frame bodies, stored once. Follows 0030.
--
-- ``POST /gfs/envelope/multi`` (v_90) hands this server ONE sealed body for
-- up to 500 mailboxes (every member household of a space reached over this
-- GFS), each with its own ``wrap`` (the body key sealed to that mailbox's
-- key-wrap key, ~150 bytes). Each mailbox drains it from its own queue, so it
-- needs a queue row — but the body is the same for all of them. It is stored
-- once in ``gfs_envelope_bodies``; each recipient's ``gfs_envelope_queue``
-- row holds a reference (``body_id``) and ITS wrap (``sealed_json``), with
-- ``size_bytes`` = body + wrap so every per-recipient cap still charges the
-- full frame. The drain pushes ``{**body, "wrap": wrap}``. The mailbox pool
-- total counts the body once plus, per reference row, 64 bytes and its wrap.
--
-- Migration audit (mandatory 3 points, CLAUDE.md).
--
--   (1) Audited every code path that already touches this data. The
--       envelope queue is written by ``SqliteGfsEnvelopeQueueRepo.enqueue``
--       (households + mailboxes, ``size_bytes = len(sealed_json)``) and
--       ``enqueue_relay`` (member relay items, ``frame_type = 'relay'``,
--       ``size_bytes`` set); read by ``list_for`` (drain) / ``count_for`` /
--       ``relay_bytes``; deleted by ``delete`` (after a delivery),
--       ``prune_expired`` (TTL), the pool expiry sweep in ``_pool_admits``,
--       ``enqueue_relay``'s evictions, and ``SqliteGfsMailboxRepo.prune_idle``
--       / ``unregister``. The pool total (``gfs_queue_totals``, 0029) is kept
--       by the 0029 triggers and recomputed hourly by
--       ``recompute_pool_totals``. Every delete path is a plain DELETE on the
--       queue, so one AFTER DELETE trigger frees an unreferenced body on all
--       of them — none has to remember it.
--
--   (2) Non-migration alternatives considered and REJECTED:
--       * one full copy of the frame per recipient (what ``/gfs/envelope``
--         does): 500 × up to ~256 KiB per space post — 128 MiB for one
--         upload — exactly the cost this removes;
--       * holding the body in memory: lost on a restart, and a cluster
--         node B draining a claim cannot read node A's memory (the nodes
--         share only this database);
--       * reusing ``gfs_media_queue`` or a relay row as the body: those are
--         per-recipient rows with their own caps and sweeps; a shared body
--         would be deleted by the first recipient's delivery.
--
--   (3) Smallest possible change: one new table, one nullable ADD COLUMN on
--       the queue (no existing row rewritten — every existing row keeps
--       ``body_id = NULL`` and behaves exactly as before), two indexes, the
--       two 0029 envelope total triggers recreated so a reference row adds
--       a fixed 64 bytes (``REF_ROW_BYTES``) plus its wrap instead of its
--       size (the body adds its size once, so the pool bounds the reference
--       rows too), two body total triggers and one cleanup trigger. The
--       wraps reuse the queue's existing ``sealed_json`` column. No backfill.
--
-- Value domain:
--   sealed_json  the verbatim ``{"mr_suite", "ciphertext"}`` body as JSON.
--                Opaque; never logged (the drain parses it only to add a
--                reference's wrap).
--   size_bytes   ``len(sealed_json)``.
--   (queue)      a reference row: ``body_id`` set, ``sealed_json`` = its
--                ``{"kem_suite", "eph_pk", "ciphertext"}`` wrap.
--   created_at / expires_at  unix seconds; the references carry the same
--                expiry, so the TTL sweep frees the body with them (the
--                body sweep in ``prune_expired`` is a backstop only).

CREATE TABLE IF NOT EXISTS gfs_envelope_bodies (
    id          INTEGER PRIMARY KEY,
    sealed_json TEXT    NOT NULL,
    size_bytes  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gfs_envelope_bodies_expiry
    ON gfs_envelope_bodies(expires_at);

ALTER TABLE gfs_envelope_queue ADD COLUMN body_id INTEGER
    REFERENCES gfs_envelope_bodies(id);

-- The cleanup trigger asks "does any row still reference this body?".
CREATE INDEX IF NOT EXISTS idx_gfs_envelope_queue_body
    ON gfs_envelope_queue(body_id) WHERE body_id IS NOT NULL;

-- ── Pool totals: the body adds its size once, a reference 64 + its wrap ──
--
-- 64 is ``REF_ROW_BYTES`` in ``global_server/repositories.py`` (the pool
-- admission in ``enqueue_multi`` and ``recompute_pool_totals`` use it): a
-- reference row holds no copy of the body, but it is still a row, and a pool
-- that never saw it could be filled with any number of them under one tiny
-- body. Its wrap (``sealed_json``) is its own bytes and counts as such.

DROP TRIGGER IF EXISTS trg_gfs_envelope_queue_total_ins;
DROP TRIGGER IF EXISTS trg_gfs_envelope_queue_total_del;

CREATE TRIGGER IF NOT EXISTS trg_gfs_envelope_queue_total_ins
AFTER INSERT ON gfs_envelope_queue
WHEN NEW.frame_type = 'envelope'
BEGIN
    UPDATE gfs_queue_totals
       SET bytes = bytes + CASE WHEN NEW.body_id IS NOT NULL
                                THEN 64 + LENGTH(NEW.sealed_json)
                                WHEN NEW.size_bytes > 0 THEN NEW.size_bytes
                                ELSE LENGTH(NEW.sealed_json) END
     WHERE queue = 'envelope' AND pool = NEW.pool;
END;

CREATE TRIGGER IF NOT EXISTS trg_gfs_envelope_queue_total_del
AFTER DELETE ON gfs_envelope_queue
WHEN OLD.frame_type = 'envelope'
BEGIN
    UPDATE gfs_queue_totals
       SET bytes = MAX(0, bytes - CASE WHEN OLD.body_id IS NOT NULL
                                       THEN 64 + LENGTH(OLD.sealed_json)
                                       WHEN OLD.size_bytes > 0 THEN OLD.size_bytes
                                       ELSE LENGTH(OLD.sealed_json) END)
     WHERE queue = 'envelope' AND pool = OLD.pool;
END;

-- Bodies exist only for mailboxes (``/gfs/envelope/multi`` delivers to
-- registered mailboxes only), so they count in the mailbox pool.
CREATE TRIGGER IF NOT EXISTS trg_gfs_envelope_bodies_total_ins
AFTER INSERT ON gfs_envelope_bodies
BEGIN
    UPDATE gfs_queue_totals SET bytes = bytes + NEW.size_bytes
     WHERE queue = 'envelope' AND pool = 'mailbox';
END;

CREATE TRIGGER IF NOT EXISTS trg_gfs_envelope_bodies_total_del
AFTER DELETE ON gfs_envelope_bodies
BEGIN
    UPDATE gfs_queue_totals SET bytes = MAX(0, bytes - OLD.size_bytes)
     WHERE queue = 'envelope' AND pool = 'mailbox';
END;

-- ── A body lives exactly as long as a reference to it ───────────────────
--
-- Delivered, tail-swept, TTL'd, unregistered, idle-swept: whichever path
-- deletes the last reference frees the body in the same statement.
CREATE TRIGGER IF NOT EXISTS trg_gfs_envelope_queue_body_free
AFTER DELETE ON gfs_envelope_queue WHEN OLD.body_id IS NOT NULL
BEGIN
    DELETE FROM gfs_envelope_bodies
     WHERE id = OLD.body_id
       AND NOT EXISTS (
           SELECT 1 FROM gfs_envelope_queue WHERE body_id = OLD.body_id
       );
END;

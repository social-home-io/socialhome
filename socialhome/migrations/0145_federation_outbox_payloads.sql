-- 0145 — one stored payload for the many recipients of one failed send.
--
-- ``federation_outbox_payloads`` holds an event's payload ONCE, wrapped
-- under the local KEK (``KeyManager.encrypt``, associated data = the row id,
-- so the database never holds it in the clear). ``id`` is an HMAC over
-- (event type, space, payload bytes) under a random per-process key that is
-- never stored, so the failed recipients of one broadcast share one row and
-- nothing guessable is kept. ``federation_outbox.payload_id`` references it:
-- such a "ref" row stores ``payload_json = ''`` and its envelope is sealed for
-- that one peer when it is sent (fresh timestamp + signature, the row id as
-- ``msg_id``). Rows queued before 0145 (``payload_id`` NULL) keep their
-- sealed envelope in ``payload_json`` and drain as before. Local-only.
--
-- Invariants live in the database, not in the repository:
--   * a ref row may only point at a stored payload (BEFORE INSERT trigger);
--   * a payload goes when its last ref row goes — delivered, failed and
--     purged, orphan-purged, evicted at the per-peer cap, or deleted with its
--     peer (AFTER DELETE trigger). No sweeper, no app-side cleanup.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the outbox: ``SqliteOutboxRepo``
--       (enqueue / supersede / cap eviction / list_due / mark_* / reschedule
--       / retention sweep / purge_terminal / purge_orphaned /
--       delete_for_instance / delete_pending_of_types / expedite),
--       ``federation_repo`` (the unpair purge's ``DELETE … WHERE
--       instance_id=?``), ``FederationService.send_event`` and
--       ``queue_event`` (the only writers), ``app._redeliver_envelope``
--       (re-signs ``payload_json``), ``SpaceModerationService
--       .outbox_entry_wanted`` (reads ``space_id`` out of ``payload_json``),
--       ``routes/admin_diagnostics`` (counts only), migration 0049's id
--       rewrite (deliberately skips ``payload_json``) and the demo harness
--       (event-type counts / deletes only). Every DELETE path is covered by
--       the trigger; the two readers of ``payload_json`` branch on
--       ``payload_id``.
--   (2) Alternatives rejected: keeping one sealed envelope per recipient
--       (today) — N × the bytes and N KEK-free pair-key ciphertexts of the
--       same content for a broadcast whose recipients are away; storing the
--       plaintext payload in ``payload_json`` of every row — still N copies
--       and in the clear; reusing ``authority_json`` — names something else;
--       an in-memory payload cache — lost on restart, and the outbox exists
--       to survive one; a JSON blob of recipients on one row — breaks the
--       per-peer cap, backoff and supersede, which are all per row.
--   (3) Smallest change: one new table, one NULL-default ``ADD COLUMN``
--       (metadata-only: no rewrite, no backfill, ``payload_json`` stays NOT
--       NULL and ref rows store ``''``), one partial index over the ref rows
--       only, and two triggers.

CREATE TABLE IF NOT EXISTS federation_outbox_payloads (
    id          TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    space_id    TEXT,
    payload_enc TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE federation_outbox ADD COLUMN payload_id TEXT;

CREATE INDEX IF NOT EXISTS idx_federation_outbox_payload
    ON federation_outbox(payload_id)
    WHERE payload_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_federation_outbox_payload_exists
BEFORE INSERT ON federation_outbox
WHEN NEW.payload_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM federation_outbox_payloads WHERE id = NEW.payload_id
 )
BEGIN
    SELECT RAISE(ABORT, 'federation_outbox: unknown payload_id');
END;

CREATE TRIGGER IF NOT EXISTS trg_federation_outbox_payload_release
AFTER DELETE ON federation_outbox
WHEN OLD.payload_id IS NOT NULL
BEGIN
    DELETE FROM federation_outbox_payloads
     WHERE id = OLD.payload_id
       AND NOT EXISTS (
           SELECT 1 FROM federation_outbox WHERE payload_id = OLD.payload_id
       );
END;

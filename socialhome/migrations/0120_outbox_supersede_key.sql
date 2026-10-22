-- 0120 — latest-only outbox rows for state that a newer send replaces.
--
-- ``federation_outbox.supersede_key`` — set (by
-- ``domain.federation_retention.supersede_key``) on an envelope that carries
-- one person's *current* state: their online status (``online:<user>``) or
-- their pin on a space map (``location:<space>:<user>``). ``enqueue`` deletes
-- the peer's pending row with the same key before it inserts, and a newer
-- state delivered directly deletes it too, so a peer that is offline for a
-- week gets one envelope per person (and space) instead of one per move or
-- flip — and those no longer evict queued DMs and posts at the per-peer cap.
-- ``NULL`` on every other row (and every pre-0120 row): kept as today.
-- Local-only, never federated.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the outbox: ``SqliteOutboxRepo``
--       (enqueue / cap eviction / list_due / retention sweep / purge /
--       delete_pending_of_types / expedite), ``FederationService.send_event``
--       and ``queue_event`` (the only writers), ``OutboxProcessor`` (re-signs
--       ``payload_json`` and redelivers it) and the senders of the three
--       kinds of state (``SpaceLocationOutbound``, ``OnlineStatusService``,
--       ``CallSignalingService``).
--   (2) Alternatives rejected: the stored envelope is sealed, so the person
--       and space a row is about cannot be read back from ``payload_json``;
--       ``delete_pending_of_types`` by type alone would drop every OTHER
--       person's latest state too; ``authority_json`` is unused but names
--       something else, and re-purposing it would mislead the next reader;
--       an in-memory map of the last queued row per key forgets on restart
--       and so leaves an older row to be delivered after a newer one; a
--       short TTL alone would lose the latest state of a peer that is away
--       longer than it (nothing re-sends it when the peer is back).
--   (3) Smallest change: one nullable ``ADD COLUMN`` (no default, no
--       backfill, no rewrite) and one partial index over the keyed rows
--       only, so the extra lookup costs nothing for every other row.

ALTER TABLE federation_outbox ADD COLUMN supersede_key TEXT;

CREATE INDEX IF NOT EXISTS idx_federation_outbox_supersede
    ON federation_outbox(instance_id, supersede_key)
    WHERE supersede_key IS NOT NULL;

-- 0151 — sync pages: the newest signed page a household holds per space.
--
-- ``docs/protocol/sync-pages.md`` (Phase 2). One row per space, the same
-- shape on the signer (the host or a seed-holding delegated admin) and on a
-- member household:
--
-- * ``page_json`` — the signed page header (canonical JSON, ≤ 1 KiB):
--   ``{space_id, epoch, signer, seq, issued_at, cutoff, rows, root,
--   prev_root, page_suite, authority_sig, authority_sig_suite}``.
-- * ``vector`` — its 256 × 32-byte bucket vector (exactly 8 192 bytes); it
--   hashes to the page's ``root``.
-- * ``delta`` — on the signer only: the buckets that changed since its
--   previous page (33 bytes each: index + hash, ≤ 256), sent to a member
--   that holds that previous page instead of the full vector. NULL on a
--   member and on a signer's first page.
--
-- No per-row data and no history: ≤ 17 KB per space on a signer, ≈ 9 KB on
-- a member (owner decision 2026-11-21: "use as little storage as
-- possible"). The row is replaced when a newer page is sealed or verified,
-- and deleted on a space-authority rotation (an old-epoch page is stale).
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path near this data. A page is new: nothing
--       stores a commitment to a space's state today. The space row
--       (``spaces``) holds the pinned authority key and epoch the page is
--       verified against and the owner statement (0093) the sentinel
--       relays — both small, signed blobs replaced by a newer one, written
--       through ``space_repo`` setters that never touch other columns.
--       ``space_instances`` (0086/0087/0150) is per (space, household):
--       the watermarks and outcomes of one provider, deleted with its seat.
--       The backup export copies neither table's sync bookkeeping.
--   (2) Non-migration alternatives considered and rejected:
--       * A column on ``spaces`` (``page_json`` + an 8 KB vector) — puts a
--         blob rewritten every 15 min (signer) or every new page (member)
--         on the hottest row of the space, read by every request that
--         loads a space; ``Space`` would carry 9 KB nobody else needs.
--       * Columns on ``space_instances`` — a page is per space, not per
--         provider (any member relays the same signed page), so it would be
--         duplicated per provider row and lost with that provider's seat.
--       * Memory only — a member would say "never" after every restart
--         until the host's next sentinel, and could not say "up to date as
--         of page N" while the host is offline (the point of Phase 2); the
--         signer could not send a delta after a restart.
--       * ``instance_config`` KV — one key per space in a settings store,
--         a binary vector as text, and no cleanup when the space goes.
--       * A per-row leaf table (option C) — 56 B × rows (2.8 MB at 50 000
--         rows); the owner chose no leaf cache (decision 4).
--   (3) Smallest possible change: one new table, three columns, keyed by
--       the space (``WITHOUT ROWID``), dropped with the space. No backfill:
--       the first BEGIN after the upgrade seals or carries the first page.

CREATE TABLE IF NOT EXISTS space_sync_pages (
    space_id   TEXT PRIMARY KEY REFERENCES spaces(id) ON DELETE CASCADE,
    page_json  TEXT NOT NULL,
    vector     BLOB NOT NULL CHECK (length(vector) = 8192),
    delta      BLOB CHECK (delta IS NULL OR length(delta) % 33 = 0)
) WITHOUT ROWID;

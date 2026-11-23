-- 0151 — sync pages: the newest signed page a household holds per space
-- and SIGNER.
--
-- ``docs/protocol/sync-pages.md`` (Phase 2). One row per (space, signer) —
-- the owner and every seed-holding delegated admin seal their own pages —
-- the same shape on a signer and on a member household:
--
-- * ``signer`` — the instance id that signed the page (``page_json``'s
--   ``signer``).
-- * ``page_json`` — the signed page header (canonical JSON, ≤ 1 KiB):
--   ``{space_id, epoch, signer, seq, issued_at, cutoff, rows, root,
--   prev_root, page_suite, authority_sig, authority_sig_suite}``.
-- * ``vector`` — its 256 × 32-byte bucket vector (exactly 8 192 bytes); it
--   hashes to the page's ``root``.
--
-- Edited in place (final review I4, 2026-11-22): an earlier draft of this
-- migration had a third column, ``delta`` (the signer's changed buckets
-- since its previous page). It almost never applied — a busy host seals
-- every 15 min and members ask every 30, so a requester rarely holds
-- exactly the previous page — and was removed with its wire field. This
-- file was never on ``main`` nor in any release (v_95 is unreleased, the
-- owner confirmed nothing from ``feat/sync-pages`` runs anywhere), so no
-- database has the old shape and no follow-up migration is needed.
--
-- Edited in place again (security re-check, 2026-11-23): keyed by
-- (space_id, signer) instead of space_id. With two signers one slot made
-- each household drop one signer's page for the other's (an owner that
-- sealed after its delegated admin never caught up the admin's page, two
-- admins replaced each other). Still never on ``main`` nor released.
--
-- No per-row data and no history: ≈ 9 KB per space and signer — k rows, k
-- = 1 + the seed-holding delegated admins, usually 1–3 (owner decision
-- 2026-11-21: "use as little storage as possible"). A row is replaced only
-- by a newer page OF THAT SIGNER, sealed or verified
-- (``space_sync_page_repo.put`` checks the order key in the same
-- statement), and every row of the space is deleted on a space-authority
-- rotation (an old-epoch page is stale).
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
--         of page N" while the host is offline (the point of Phase 2), nor
--         prove rows to another member (Phase 3).
--       * ``instance_config`` KV — one key per space in a settings store,
--         a binary vector as text, and no cleanup when the space goes.
--       * A per-row leaf table (option C) — 56 B × rows (2.8 MB at 50 000
--         rows); the owner chose no leaf cache (decision 4).
--   (3) Smallest possible change: one new table, two data columns, keyed by
--       (space, signer) (``WITHOUT ROWID``), dropped with the space. No backfill:
--       the first BEGIN after the upgrade seals or carries the first page.

CREATE TABLE IF NOT EXISTS space_sync_pages (
    space_id   TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    signer     TEXT NOT NULL,
    page_json  TEXT NOT NULL,
    vector     BLOB NOT NULL CHECK (length(vector) = 8192),
    PRIMARY KEY (space_id, signer)
) WITHOUT ROWID;

-- A space-authority rotation retires the key every held page was signed
-- with: the page goes in the same statement that moves the epoch or the
-- pinned key, on every path that moves them (the owner's
-- ``rotate_authority_key``, a member's ``adopt_authority_key``, a re-pin
-- that keeps the epoch — ``set_space_pubkey``, ``adopt_gfs_pin``). The
-- next BEGIN seals or carries a new one.
CREATE TRIGGER IF NOT EXISTS spaces_rotation_drops_sync_page
AFTER UPDATE OF authority_key_epoch, identity_public_key ON spaces
WHEN NEW.authority_key_epoch IS NOT OLD.authority_key_epoch
  OR NEW.identity_public_key IS NOT OLD.identity_public_key
BEGIN
    DELETE FROM space_sync_pages WHERE space_id = NEW.id;
END;

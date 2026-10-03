-- Space page tombstones (§25.6 sync / resume) — the page twin of the task
-- tombstones in 0071.
--
-- 1. ``space_pages.deleted_at`` — a space page delete keeps the row as a
--    tombstone instead of removing it. Without it a household that missed
--    ``SPACE_PAGE_DELETED`` (offline past the outbox, or the envelope lost)
--    kept the page forever, and its §25.6 ``pages`` stream or its resume
--    replay re-added the page on a household that never held it (a member
--    takes a new page from any member household, unsequenced), and the
--    deleted text lived on. The host now streams its tombstones
--    (``pages_deleted``), the resume replay re-sends deletes since
--    ``since``, and no upsert ever brings a tombstoned id back. NULL = live,
--    so every existing row stays live. A tombstone keeps no content: the
--    delete blanks title / content / cover (the user deleted that text).
-- 2. ``space_pages.deleted_by`` — the user who authorised the delete (the
--    approver of a reviewed one). Shipped as ``actor_user_id`` with the
--    streamed tombstone and the replayed ``SPACE_PAGE_DELETED``, so a
--    receiver judges it against the space's ``pages`` access level like a
--    live delete. NULL = live, or nobody we can name.
-- 3. Trigger ``space_pages_tombstone_drops_bodies`` — on the live →
--    tombstoned transition the page's conflict sides, draft base and
--    resolved snapshots (``space_page_snapshots``) and its edit history
--    (``page_edit_history``) in that space go too. Neither table has a FK
--    to ``space_pages`` (a household page shares them, scoped by a NULL
--    ``space_id``), so ``ON DELETE CASCADE`` was never an option — and the
--    row is no longer deleted anyway. The old hard delete dropped the
--    snapshots by hand and leaked the history; the trigger makes "a
--    tombstone holds no body anywhere" a database invariant, whichever
--    code path tombstones.
--
-- Audit: every reader / writer of ``space_pages`` is ``SqlitePageRepo``
-- (``grep -rn space_pages socialhome``: page_repo.py only, plus the
-- whole-table backup / export dumps, which copy the new columns as-is).
-- Its live reads (``get``, ``get_space_page``, ``list``, ``list_since``,
-- ``list_pending_drafts``) now skip tombstones, so every service, route,
-- exporter, the resume provider and the sequencer above it sees a deleted
-- page as absent — exactly as the hard delete left it. Its writes refuse a
-- tombstone: the shared upsert (``save``, ``commit_version``) carries
-- ``deleted_at IS NULL`` in its ON CONFLICT ... WHERE, so a sequenced
-- commit, a mirror, a sync record, a resume replay or a late proposal
-- naming a deleted id writes nothing (the host answers ``gone``). The
-- sequencer's history / side writes run under the page lock the delete
-- also takes. ``page_edit_history`` / ``space_page_snapshots`` are read
-- per (page, space) only.
-- Alternatives rejected: a separate ``space_page_tombstones`` table (a
-- second home for the same id — the one PRIMARY KEY's ON CONFLICT is what
-- makes "a tombstone wins" a single atomic statement, and 0069 / 0071 set
-- the in-row shape); the in-memory ``GalleryAlbumTombstones`` shape
-- (forgets on restart, cannot be streamed to a household offline across
-- the delete); a ``deleted`` flag carried on the ``pages`` sync records
-- (an older receiver would read a tombstone as a live page — the separate
-- ``pages_deleted`` resource is dropped as unknown instead). A stub
-- tombstone for an id never held is written only from the host and only
-- for an id owner-bound to its creator in this space (page ids are
-- global). Tombstones are never pruned (like 0069 / 0071).
--
-- Smallest change: two NULL-default columns (metadata-only in SQLite — no
-- table rewrite, no backfill) and one trigger; no new index (tombstone
-- reads are per space, served by ``idx_space_pages_space``). No existing
-- row changes: the trigger fires only on a later live → tombstoned
-- transition; pages hard-deleted before 0073 are gone already.

ALTER TABLE space_pages ADD COLUMN deleted_at TEXT;
ALTER TABLE space_pages ADD COLUMN deleted_by TEXT;

CREATE TRIGGER space_pages_tombstone_drops_bodies
AFTER UPDATE OF deleted_at ON space_pages
WHEN NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL
BEGIN
    DELETE FROM space_page_snapshots
     WHERE page_id = NEW.id AND space_id = NEW.space_id;
    DELETE FROM page_edit_history
     WHERE page_id = NEW.id AND space_id = NEW.space_id;
END;

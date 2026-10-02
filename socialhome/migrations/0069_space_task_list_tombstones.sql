-- Space task-list tombstones + rename stamp (§25.6 sync / resume).
--
-- 1. ``space_task_lists.deleted_at`` — a delete keeps the row as a
--    tombstone instead of removing it. Without it a household that
--    missed ``SPACE_TASK_LIST_DELETED`` (offline past the outbox, or the
--    event dropped) kept the list forever, and its §25.6 stream re-added
--    the list on every household that had deleted it. The host's sync
--    stream now ships the tombstones (``task_lists_deleted``), the resume
--    replay re-sends deletes since ``since``, and an upsert never brings
--    a tombstoned id back. Same shape as ``space_timetables.deleted_at``
--    (0063). NULL = live, so every existing row stays live.
-- 2. ``space_task_lists.deleted_by`` — the user who deleted the list.
--    Shipped as ``actor_user_id`` with the streamed tombstone and the
--    replayed ``SPACE_TASK_LIST_DELETED``, so a receiver judges the delete
--    against the space's ``tasks`` access level like a live one; without
--    it an ``ADMIN_ONLY`` / ``MODERATED`` space refused every replayed
--    delete (v_42 receivers refuse an actor-less one) and never converged.
--    NULL = live, or deleted by nobody we can name (an older peer).
-- 3. ``space_task_lists.updated_at`` — when the list was last renamed.
--    The resume replay sends lists created OR renamed since ``since``;
--    with only ``created_at`` a missed rename was never replayed, and
--    re-sending every list instead would let a co-member that missed a
--    rename revert it. NULL = never renamed (``created_at`` stands).
-- 4. Trigger ``space_task_lists_tombstone_drops_tasks`` — tombstoning a
--    list deletes its tasks, exactly what the ``list_id`` FK's
--    ON DELETE CASCADE did for the hard delete it replaces, so every
--    writer (local delete, live inbound, sync, resume) cascades alike.
--
-- Audit: every reader / writer of ``space_task_lists`` is the
-- ``SqliteSpaceTaskRepo`` (plus the whole-table backup / export dumps,
-- which copy the new columns as-is); its reads now skip tombstones.
-- Alternatives rejected: the in-memory ``GalleryAlbumTombstones`` shape
-- forgets every delete on restart and cannot be streamed to a household
-- that was offline across one; replaying every list on resume (no
-- ``updated_at``) reverts newer renames from a stale co-member; naming
-- the deleter from ``created_by`` is wrong whenever someone else (an
-- admin, an approving moderator) deleted the list, and the live event
-- that carried the actor is gone by the time a replay needs it. A stub
-- tombstone for an id never held is written only when the id is
-- owner-bound to this space (``created_by`` in the record), since ids are
-- global. Tombstones are never pruned (like ``space_timetables``).
--
-- All additive: three NULL-default columns (metadata-only in SQLite — no
-- table rewrite, no backfill) and one trigger that fires only on a
-- live → tombstoned transition.

ALTER TABLE space_task_lists ADD COLUMN deleted_at TEXT;
ALTER TABLE space_task_lists ADD COLUMN deleted_by TEXT;
ALTER TABLE space_task_lists ADD COLUMN updated_at TEXT;

CREATE TRIGGER space_task_lists_tombstone_drops_tasks
AFTER UPDATE OF deleted_at ON space_task_lists
WHEN NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL
BEGIN
    DELETE FROM space_tasks WHERE list_id = NEW.id;
END;

-- Space task tombstones (§25.6 sync / resume) — the single-task twin of
-- the task-LIST tombstones in 0069.
--
-- 1. ``space_tasks.deleted_at`` — a single-task delete keeps the row as a
--    tombstone instead of removing it. Without it a household that missed
--    ``SPACE_TASK_DELETED`` (offline past the outbox, or the event dropped)
--    kept the task forever, and its §25.6 ``tasks`` / ``tasks_archived``
--    stream (or a re-sent ``SPACE_TASK_UPDATED``) re-added the task on
--    every household that had deleted it. The host's sync stream now ships
--    the tombstones (``tasks_deleted``), the resume replay re-sends deletes
--    since ``since``, and an upsert never brings a tombstoned id back.
--    NULL = live, so every existing row stays live. A tombstone keeps no
--    content: the delete blanks the title / description / assignees /
--    labels / priority / due date / recurrence (the user deleted that text).
-- 2. ``space_tasks.deleted_by`` — the user who authorised the delete (the
--    approver of a reviewed one). Shipped as ``actor_user_id`` with the
--    streamed tombstone and the replayed ``SPACE_TASK_DELETED``, so a
--    receiver judges it against the space's ``tasks`` access level like a
--    live delete; v_42 receivers refuse an actor-less delete under
--    ``ADMIN_ONLY`` / ``MODERATED``. NULL = live, or nobody we can name.
-- 3. Trigger ``space_task_lists_tombstone_drops_tasks`` is recreated (0069
--    has shipped and is not edited): a list tombstone now tombstones the
--    list's live tasks IN PLACE (``deleted_at`` / ``deleted_by`` from the
--    list, content blanked) instead of deleting the rows. The 0069 hard
--    delete removed every trace of the task id, so its owner household
--    could re-file the same id under another live list of the space (a
--    live create, a stream, or a plain upsert). Keeping the row makes the
--    id a tombstone like any other — ``save``'s ON CONFLICT refuses it.
--    Tasks tombstoned before the list keep their own ``deleted_by``.
--
-- Audit: every reader / writer of ``space_tasks`` is ``SqliteSpaceTaskRepo``
-- (``grep -rn space_tasks socialhome``: task_repo.py only, plus the 0069
-- trigger and the whole-table backup / export dumps, which copy the new
-- columns as-is). Its reads (``get``, ``list_by_list``, ``list_by_space``,
-- ``list_since``, ``open_counts``, ``next_position``) now skip tombstones,
-- so every service / route / exporter above it sees a deleted task as
-- absent; ``open_counts`` and the ``tasks`` / ``tasks_archived`` exporters
-- read through them, so a task tombstoned by its list's delete is gone
-- there exactly as the 0069 hard delete left it. Tasks under a deleted
-- list are NOT streamed / replayed as task tombstones
-- (``list_task_tombstones`` skips them): the list's own tombstone already
-- tells a household that missed it, and its trigger tombstones them there
-- too — no double bookkeeping on the wire. ``task_comments`` / ``task_attachments`` have no FK
-- to ``space_tasks`` and the old hard delete never touched them either.
-- Alternatives rejected: a separate ``space_task_tombstones`` table (a
-- second home for the same id — ``save``'s ON CONFLICT on the one PRIMARY
-- KEY is what makes "a tombstone wins" a single atomic statement, and
-- 0069 already set the in-row shape for lists); the in-memory
-- ``GalleryAlbumTombstones`` shape (forgets on restart, cannot be streamed
-- to a household that was offline across the delete); deriving the
-- deleter from ``created_by`` (wrong whenever an admin or an approving
-- moderator deleted someone else's task). A stub tombstone for an id never
-- held is written only from the host, only for an id owner-bound to this
-- space, and only under a list live here in this space (``list_id`` is a
-- FK). Tombstones are never pruned (like 0069 / ``space_timetables``).
--
-- Smallest change: two NULL-default columns (metadata-only in SQLite — no
-- table rewrite, no backfill), no new index (tombstone reads are per
-- space, served by ``idx_space_tasks_space``), and one trigger swapped
-- for its in-place twin. No existing row changes: the trigger fires only
-- on a later live → tombstoned list transition, and the tasks of lists
-- already tombstoned under 0069 were deleted then (nothing to backfill).

ALTER TABLE space_tasks ADD COLUMN deleted_at TEXT;
ALTER TABLE space_tasks ADD COLUMN deleted_by TEXT;

DROP TRIGGER IF EXISTS space_task_lists_tombstone_drops_tasks;

CREATE TRIGGER space_task_lists_tombstone_drops_tasks
AFTER UPDATE OF deleted_at ON space_task_lists
WHEN NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL
BEGIN
    UPDATE space_tasks
       SET deleted_at = NEW.deleted_at,
           deleted_by = NEW.deleted_by,
           title = '',
           description = NULL,
           due_date = NULL,
           assignees_json = '[]',
           labels_json = '[]',
           priority = NULL,
           rrule = NULL,
           last_spawned_at = NULL,
           recurrence_parent_id = NULL
     WHERE list_id = NEW.id AND deleted_at IS NULL;
END;

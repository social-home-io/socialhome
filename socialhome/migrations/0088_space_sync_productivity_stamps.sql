-- 0088 — §25.6 incremental sync for the productivity resources: the 0086
-- change stamp (``sync_seq``) on ``space_task_lists``, ``space_tasks``,
-- ``space_pages`` and ``space_timetables``. A periodic session then streams
-- only the task lists, tasks (active and archived), pages, timetables and
-- their tombstones (``task_lists_deleted``, ``tasks_deleted``,
-- ``pages_deleted``) changed since the household's watermark, instead of
-- every one of them every 30 minutes.
--
-- 1. ``sync_seq INTEGER`` (NULL default) on the four tables, with the 0086
--    trigger pair: ``AFTER INSERT`` stamps from ``sync_seq_counter``;
--    ``AFTER UPDATE`` stamps only when a synced column changes (row-value
--    ``IS NOT`` — an idempotent re-apply stamps nothing, so no echo between
--    households) or when the stamp is set to NULL (a touch). Left out of the
--    comparison: ``updated_at`` (local bookkeeping / the writer's clock; a
--    real edit always changes a content column too). A tombstone is an
--    update (``deleted_at`` set, content blanked), so it stamps — the
--    tombstone resources filter their existing ``(deleted_at, id)`` keyset
--    by the stamp; no new keyset. The 0069 / 0071 list-tombstone trigger
--    tombstones the list's tasks with UPDATEs, so they stamp too.
-- 2. Touch triggers on ``space_page_snapshots``: a page's streamed record
--    includes its draft base (``side='base'``) and open conflict sides
--    (``conflict=1``), both rows of this table. An insert / update / delete
--    of a SPACE page's snapshot sets the page's ``sync_seq`` to NULL and the
--    page's update trigger re-stamps it (the 0086 post-child pattern).
--    Household snapshots (``space_id`` NULL) touch nothing. The third page
--    table, ``page_edit_history``, is not part of any synced record and
--    stamps nothing.
-- 3. No index: these tables are small and per space; the existing
--    ``(space_id)`` indexes serve the reads.
--
-- Existing rows stay NULL (no backfill). ``SYNC_SHAPE_VERSION`` goes to 2
-- with this change, so every household's watermark — taken before these
-- rows could be stamped — is invalidated and its next session streams in
-- full once; a row edited between that watermark and this upgrade has no
-- stamp and would otherwise be skipped.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data.
--       ``grep -rn "space_task_lists\|space_tasks\|space_pages\|
--       space_page_snapshots\|space_timetables" socialhome``: writes live in
--       ``task_repo`` (SqliteSpaceTaskRepo: ``save_list`` / ``save`` upserts
--       with ``ON CONFLICT DO UPDATE``, ``delete_list`` / ``delete``
--       tombstone UPDATEs, ``tombstone_list`` / ``tombstone`` insert-only
--       stubs), ``page_repo`` (the ``_SPACE_PAGE_UPSERT`` upsert, tombstone /
--       ``confirm_delete`` / ``revive`` / ``raise_seq`` UPDATEs, lock sweeps,
--       ``set_conflict_sides`` / ``set_draft_base`` / ``commit_version`` /
--       ``insert_snapshot`` on ``space_page_snapshots``, partly via ``INSERT
--       OR REPLACE`` — whose insert fires the touch), ``timetable_repo``
--       (SqliteSpaceTimetableRepo: insert, compare-and-swap ``save``,
--       last-writer-wins ``apply_remote``, ``soft_delete`` / ``tombstone``)
--       and the 0069 / 0071 / 0073 tombstone triggers — reached from the
--       local services, the federation inbound handlers, the resume replay
--       and the §25.6 receiver. No covered table is written with ``INSERT
--       OR REPLACE`` (which would re-stamp on an idempotent apply). The
--       exporters read through ``list_lists`` / ``list_by_space`` /
--       ``list`` / ``list_*_tombstones``, which gain a ``since_seq`` filter.
--       The receiver applies these records by id with no "absent means
--       deleted" reconciliation, so a stream of only the changed rows is
--       safe. ``updated_at`` is written by the repos (task / page / timetable
--       upserts) but no comparison needs it.
--   (2) Non-migration alternatives considered and rejected:
--       * Read-time change detection from ``updated_at`` — wall-clock, mixed
--         naive / tz-aware forms (pages), not bumped by every change (a list
--         rename only sometimes, a task archive yes, a snapshot never), and a
--         receiver writes the sender's value, so it says nothing about when
--         the row changed HERE (0086 rejected the same for posts).
--       * Keep streaming them in full (status quo) — every task, page and
--         timetable re-read, re-encrypted and re-applied per peer × space
--         every 30 minutes.
--       * A change-log table — see 0086: a second home for every row id.
--   (3) Smallest possible change: four NULL-default columns (metadata-only,
--       no rewrite, no backfill) and triggers. Nothing existing is rewritten
--       or dropped.

ALTER TABLE space_task_lists ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER space_task_lists_sync_seq_insert
AFTER INSERT ON space_task_lists
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_task_lists SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER space_task_lists_sync_seq_update
AFTER UPDATE ON space_task_lists
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.name, NEW.created_by, NEW.created_at, NEW.deleted_at, NEW.deleted_by)
   IS NOT (OLD.id, OLD.space_id, OLD.name, OLD.created_by, OLD.created_at, OLD.deleted_at, OLD.deleted_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_task_lists SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE space_tasks ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER space_tasks_sync_seq_insert
AFTER INSERT ON space_tasks
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_tasks SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER space_tasks_sync_seq_update
AFTER UPDATE ON space_tasks
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.list_id, NEW.space_id, NEW.title, NEW.description, NEW.due_date, NEW.assignees_json, NEW.status, NEW.position, NEW.created_by, NEW.rrule, NEW.last_spawned_at, NEW.recurrence_parent_id, NEW.archived_at, NEW.created_at, NEW.priority, NEW.labels_json, NEW.deleted_at, NEW.deleted_by)
   IS NOT (OLD.id, OLD.list_id, OLD.space_id, OLD.title, OLD.description, OLD.due_date, OLD.assignees_json, OLD.status, OLD.position, OLD.created_by, OLD.rrule, OLD.last_spawned_at, OLD.recurrence_parent_id, OLD.archived_at, OLD.created_at, OLD.priority, OLD.labels_json, OLD.deleted_at, OLD.deleted_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_tasks SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE space_pages ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER space_pages_sync_seq_insert
AFTER INSERT ON space_pages
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_pages SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER space_pages_sync_seq_update
AFTER UPDATE ON space_pages
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.title, NEW.content, NEW.cover_image_url, NEW.created_by, NEW.created_at, NEW.last_editor_user_id, NEW.last_edited_at, NEW.locked_by, NEW.locked_at, NEW.lock_expires_at, NEW.delete_requested_by, NEW.delete_requested_at, NEW.delete_approved_by, NEW.delete_approved_at, NEW.seq, NEW.pending_base_seq, NEW.deleted_at, NEW.deleted_by, NEW.delete_confirmed)
   IS NOT (OLD.id, OLD.space_id, OLD.title, OLD.content, OLD.cover_image_url, OLD.created_by, OLD.created_at, OLD.last_editor_user_id, OLD.last_edited_at, OLD.locked_by, OLD.locked_at, OLD.lock_expires_at, OLD.delete_requested_by, OLD.delete_requested_at, OLD.delete_approved_by, OLD.delete_approved_at, OLD.seq, OLD.pending_base_seq, OLD.deleted_at, OLD.deleted_by, OLD.delete_confirmed))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_pages SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE space_timetables ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER space_timetables_sync_seq_insert
AFTER INSERT ON space_timetables
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_timetables SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER space_timetables_sync_seq_update
AFTER UPDATE ON space_timetables
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.name, NEW.color, NEW.week_start, NEW.tz, NEW.days_json, NEW.defaults_json, NEW.entries_json, NEW.overrides_json, NEW.valid_from, NEW.valid_until, NEW.excluded_weeks_json, NEW.version, NEW.created_by, NEW.updated_by, NEW.created_at, NEW.deleted_at)
   IS NOT (OLD.id, OLD.space_id, OLD.name, OLD.color, OLD.week_start, OLD.tz, OLD.days_json, OLD.defaults_json, OLD.entries_json, OLD.overrides_json, OLD.valid_from, OLD.valid_until, OLD.excluded_weeks_json, OLD.version, OLD.created_by, OLD.updated_by, OLD.created_at, OLD.deleted_at))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_timetables SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

CREATE TRIGGER space_page_snapshots_touch_page_insert
AFTER INSERT ON space_page_snapshots
WHEN NEW.space_id IS NOT NULL
BEGIN
    UPDATE space_pages SET sync_seq = NULL
     WHERE id = NEW.page_id AND space_id = NEW.space_id;
END;
CREATE TRIGGER space_page_snapshots_touch_page_update
AFTER UPDATE ON space_page_snapshots
WHEN (NEW.page_id, NEW.space_id, NEW.snapshot_at, NEW.body, NEW.snapshot_by, NEW.side, NEW.conflict)
 IS NOT (OLD.page_id, OLD.space_id, OLD.snapshot_at, OLD.body, OLD.snapshot_by, OLD.side, OLD.conflict)
BEGIN
    UPDATE space_pages SET sync_seq = NULL
     WHERE id = NEW.page_id AND space_id = NEW.space_id;
    UPDATE space_pages SET sync_seq = NULL
     WHERE id = OLD.page_id AND space_id = OLD.space_id
       AND (OLD.page_id, OLD.space_id) IS NOT (NEW.page_id, NEW.space_id);
END;
CREATE TRIGGER space_page_snapshots_touch_page_delete
AFTER DELETE ON space_page_snapshots
WHEN OLD.space_id IS NOT NULL
BEGIN
    UPDATE space_pages SET sync_seq = NULL
     WHERE id = OLD.page_id AND space_id = OLD.space_id;
END;

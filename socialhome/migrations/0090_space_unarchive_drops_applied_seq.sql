-- 0090 — Lifting a space's archive drops this household's §25.6 echo for
-- it, so the next periodic sync from every provider streams in full.
--
-- While a space is (reversibly) archived here, the sync receiver refuses a
-- member household's content records by rule (``archive_refusal`` — the
-- snapshot is read-only), and the stream still counts clean: refusing them
-- again on every stream must not turn every periodic session into a full
-- one for as long as the archive lasts. So the provider's watermark and
-- our echo (``space_instances.applied_seq``, migration 0087) advance past
-- those rows. Once the archive is lifted they would wait for the daily
-- full pass. Trigger ``spaces_unarchive_drops_applied_seq``: when
-- ``spaces.archived`` goes 1 → 0, NULL the space's ``applied_seq`` on
-- every ``space_instances`` row — the next periodic BEGIN carries no
-- ``have_seq`` and the provider streams the whole window (the existing
-- fail-safe), delivering what the archive refused.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every path that lifts an archive: ``SpaceService
--       .unarchive_space`` → ``set_archived(id, False)``; the member's copy
--       of a host's ``SPACE_CONFIG_CHANGED`` (unarchived) and a refreshed
--       space snapshot, which upsert ``spaces.archived`` from the host's
--       metadata (``SqliteSpaceRepo`` upsert ``archived=excluded.archived``).
--       A terminated space (``archived_reason`` set) never goes back to 0.
--       ``applied_seq`` is written only by ``record_applied`` after a clean
--       stream and read only by the scheduler's ``begin_fields``.
--   (2) Non-migration alternatives considered and rejected: clearing the
--       echo in each service path (three writers today, any new one would
--       silently reintroduce the residual — the owner wants the invariant in
--       the database); counting archive refusals as "retry" (the stream
--       would never confirm, so its watermark and daily-full stamp freeze
--       and every 30-minute session becomes full for the whole archive);
--       storing the archive state with the echo (a new column for what one
--       transition already tells).
--   (3) Smallest change: one AFTER UPDATE trigger, no column, no index, no
--       backfill; it only NULLs a NULL-able bookkeeping column whose NULL
--       already means "stream in full".

CREATE TRIGGER spaces_unarchive_drops_applied_seq
AFTER UPDATE OF archived ON spaces
WHEN OLD.archived = 1 AND NEW.archived = 0
BEGIN
    UPDATE space_instances SET applied_seq = NULL
     WHERE space_id = NEW.id AND applied_seq IS NOT NULL;
END;

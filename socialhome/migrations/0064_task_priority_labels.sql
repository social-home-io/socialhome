-- Task priority + labels — household ``tasks`` and ``space_tasks``.
--
-- 1. ``priority`` — optional urgency (low / medium / high / urgent). NULL
--    means "no priority", so every existing row stays exactly as it was.
-- 2. ``labels_json`` — the task's free-text labels as a JSON array of
--    strings (at most 10 of at most 32 characters, normalised by
--    ``socialhome.domain.task.normalize_labels``). Same storage shape as
--    ``assignees_json``.
--
-- All additive: four columns with a NULL / constant default (metadata-only
-- in SQLite — no table rewrite, no backfill).

ALTER TABLE tasks ADD COLUMN priority TEXT
    CHECK(priority IN ('low','medium','high','urgent'));
ALTER TABLE tasks ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE space_tasks ADD COLUMN priority TEXT
    CHECK(priority IN ('low','medium','high','urgent'));
ALTER TABLE space_tasks ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]';

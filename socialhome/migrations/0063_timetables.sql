-- Timetable module (school *Stundenplan*) — household + space scope.
--
-- 1. ``timetables`` — household timetables. One row per timetable; the
--    weekly grid (entries), per-date changes (overrides), defaults, days,
--    excluded weeks and assignees are small bounded lists stored as JSON
--    in the exact element shape of the domain wire format
--    (``socialhome.domain.timetable``), so the repository and the REST /
--    federation body can't drift. ``version`` is the compare-and-swap
--    counter every edit bumps.
-- 2. ``space_timetables`` — the same shape scoped to a space (no
--    assignees). ``deleted_at`` is a tombstone: a replayed federation
--    upsert must not resurrect a deleted timetable, so a delete clears the
--    content and keeps the row. Cascades away with the space.
-- 3. ``preferences.feat_timetable`` — household feature toggle, default ON
--    like the other ``feat_*`` columns.
-- 4. ``spaces.feature_timetable`` — per-space toggle, default OFF: a new
--    tab stays hidden in existing spaces until an admin turns it on.
--
-- All additive: two new tables and two constant-default columns
-- (metadata-only in SQLite — no table rewrite, no backfill).

CREATE TABLE timetables (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    color               TEXT,
    week_start          INTEGER NOT NULL DEFAULT 0 CHECK(week_start IN (0, 6)),
    tz                  TEXT NOT NULL DEFAULT 'UTC',
    days_json           TEXT NOT NULL DEFAULT '[0,1,2,3,4]',
    defaults_json       TEXT NOT NULL DEFAULT '{}',
    entries_json        TEXT NOT NULL DEFAULT '[]',
    overrides_json      TEXT NOT NULL DEFAULT '[]',
    valid_from          TEXT,
    valid_until         TEXT,
    excluded_weeks_json TEXT NOT NULL DEFAULT '[]',
    assignees_json      TEXT NOT NULL DEFAULT '[]',
    version             INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by          TEXT NOT NULL,
    updated_by          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE space_timetables (
    id                  TEXT PRIMARY KEY,
    space_id            TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    color               TEXT,
    week_start          INTEGER NOT NULL DEFAULT 0 CHECK(week_start IN (0, 6)),
    tz                  TEXT NOT NULL DEFAULT 'UTC',
    days_json           TEXT NOT NULL DEFAULT '[0,1,2,3,4]',
    defaults_json       TEXT NOT NULL DEFAULT '{}',
    entries_json        TEXT NOT NULL DEFAULT '[]',
    overrides_json      TEXT NOT NULL DEFAULT '[]',
    valid_from          TEXT,
    valid_until         TEXT,
    excluded_weeks_json TEXT NOT NULL DEFAULT '[]',
    version             INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by          TEXT NOT NULL,
    updated_by          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    -- Tombstone: a replayed upsert must not resurrect a deleted timetable.
    deleted_at          TEXT
);

CREATE INDEX idx_space_timetables_space ON space_timetables(space_id);

ALTER TABLE preferences
    ADD COLUMN feat_timetable INTEGER NOT NULL DEFAULT 1
        CHECK(feat_timetable IN (0, 1));

ALTER TABLE spaces
    ADD COLUMN feature_timetable INTEGER NOT NULL DEFAULT 0
        CHECK(feature_timetable IN (0, 1));

-- 0067 — a content report knows which space it is about.
--
-- Three changes to ``content_reports``, one rebuild:
--
--   * a nullable ``space_id`` column: the space the reported content (or
--     the reported member) lives in. NULL = a household-level report
--     (feed content, a user outside any space, a space itself), triaged
--     by household admins exactly as before. Non-NULL = the space's
--     content authority (owner / admin / moderator) triages it, and
--     household admins do not see it.
--   * a nullable ``sole_reviewer_user_id`` column: the subject of a space
--     report who, when it was filed, was the space's owner and its only
--     content authority — the only person allowed the anonymous,
--     dismiss-only fallback. Decided at filing (not at review) so an owner
--     cannot demote every other moderator afterwards to qualify. No
--     existing column could carry it: ``resolved_by`` is the decider,
--     ``reporter_instance_id`` the sender.
--   * the ``target_type`` CHECK admits the space content kinds that had
--     no report target until now: 'page', 'task', 'sticky',
--     'calendar_event', 'gallery_item'.
--
-- Why a rebuild (not an ALTER): the ``space_id`` column alone would be an
-- ADD COLUMN, but SQLite cannot edit a CHECK in place; the documented
-- "create new → copy → drop → rename" dance is the only route
-- (https://sqlite.org/lang_altertable.html, §"Making Other Kinds Of Table
-- Schema Changes"), the same procedure as 0042 / 0054 / 0065. The table is
-- rebuilt EXACTLY as 0001 created it (no later migration touched it) plus
-- the two changes; two of its indexes are reproduced verbatim, the
-- one-report-per-target UNIQUE index gains the scope, and one is added.
-- It has no foreign keys, no triggers, and nothing references it.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data:
--       ``repositories/report_repo.py`` is the only SQL on the table
--       (save / get / list_by_status / count_recent_by_reporter / resolve
--       / has_open_for_target, the last read by
--       ``services/relay_policy.py``); it is written by
--       ``services/report_service.py`` (local filing + the inbound
--       ``SPACE_REPORT`` handler in ``federation_inbound_service.py``) and
--       read by the admin + new space report routes. Backups dump it
--       generically.
--   (2) Non-migration alternatives considered and rejected: deriving the
--       space at read time from the target works for posts / comments but
--       NOT for a reported member (a user is in many spaces — the report
--       is about their conduct in ONE), and listing a space's reports
--       would mean resolving every pending report's target on each read.
--       Encoding the kind inside ``target_id`` (e.g. 'page:<id>' under
--       target_type 'post') would lie to ``has_open_for_target`` and every
--       reader. The space also cannot live in a federation event only —
--       the receiving household must store which space it triages for.
--   (3) Smallest possible change: two NULL-defaulted columns (no backfill
--       — pre-0067 rows stay household-level, which is who already saw
--       them), five values added to one CHECK, one index added, the
--       UNIQUE pair widened by the scope. Every existing
--       value is copied by explicit column list.
PRAGMA foreign_keys=OFF;

CREATE TABLE content_reports_new (
    id                    TEXT PRIMARY KEY,
    target_type           TEXT NOT NULL
                          CHECK(target_type IN ('post','comment','user','space',
                                                'highlight','moment',
                                                'page','task','sticky',
                                                'calendar_event','gallery_item')),
    target_id             TEXT NOT NULL,
    reporter_user_id      TEXT NOT NULL,
    reporter_instance_id  TEXT,
    category              TEXT NOT NULL
                          CHECK(category IN ('spam','harassment',
                                              'inappropriate','misinformation',
                                              'other')),
    notes                 TEXT,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK(status IN ('pending','resolved','dismissed')),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_by           TEXT,
    resolved_at           TEXT,
    -- NULL = household-level report (household admins triage it).
    space_id              TEXT,
    -- The report's subject when, at FILING time, they were the space's
    -- owner and its only content authority anywhere — the one person who
    -- may later dismiss it anonymously. NULL = nobody may (pinned at
    -- filing so an owner cannot demote everyone afterwards to qualify).
    sole_reviewer_user_id TEXT
);
INSERT INTO content_reports_new
    (id, target_type, target_id, reporter_user_id, reporter_instance_id,
     category, notes, status, created_at, resolved_by, resolved_at)
SELECT
    id, target_type, target_id, reporter_user_id, reporter_instance_id,
    category, notes, status, created_at, resolved_by, resolved_at
FROM content_reports;
DROP TABLE content_reports;
ALTER TABLE content_reports_new RENAME TO content_reports;

-- DROP TABLE took the indexes with it; recreate two verbatim (0001).
CREATE INDEX IF NOT EXISTS idx_content_reports_status
    ON content_reports(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_reports_reporter
    ON content_reports(reporter_user_id, created_at DESC);
-- One report per (reporter, target) PER SCOPE: the same member may be
-- reported once in each space they misbehave in. ``COALESCE`` keeps the
-- household scope (NULL) unique too — NULLs never collide in a plain
-- UNIQUE index, which would let duplicates through.
CREATE UNIQUE INDEX IF NOT EXISTS idx_content_reports_unique_pair
    ON content_reports(reporter_user_id, target_type, target_id,
                       COALESCE(space_id, ''));
-- New: a space's report queue.
CREATE INDEX IF NOT EXISTS idx_content_reports_space
    ON content_reports(space_id, status, created_at DESC);

PRAGMA foreign_key_check;
PRAGMA foreign_keys=ON;

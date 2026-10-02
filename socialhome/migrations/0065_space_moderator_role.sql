-- 0065 — a space seat may be a ``moderator`` (federation v_41).
--
-- Widens both space role CHECKs by one value:
--
--   space_members.role         owner, admin, member, subscriber
--                              → owner, admin, moderator, member, subscriber
--   space_remote_members.role  member, admin, subscriber
--                              → member, admin, moderator, subscriber
--
-- A moderator holds CONTENT authority (approve / reject the moderation
-- queue, edit / delete / pin other people's content) and no SETTINGS
-- authority at all. The in-code authority is ``SpaceRole`` plus the
-- ``SETTINGS_AUTHORITY_ROLES`` / ``CONTENT_AUTHORITY_ROLES`` /
-- ``WRITER_ROLES`` sets in ``domain/space.py``; these CHECKs are the
-- on-disk authority and must admit the same vocabulary.
--
-- Why a rebuild (not an ALTER): SQLite cannot alter a CHECK constraint in
-- place; the documented "create new table → copy → drop → rename" dance is
-- the only route (https://sqlite.org/lang_altertable.html, §"Making Other
-- Kinds Of Table Schema Changes"). Same procedure as 0042 and 0054. Each
-- table is rebuilt EXACTLY as it exists after 0064 — ``space_members`` is
-- the 0001 shape (no later ALTER), ``space_remote_members`` is the 0054
-- shape — changing ONLY the CHECK; the FK, the PK and the one index on each
-- are reproduced verbatim. Neither table has a trigger, and no table holds a
-- foreign key INTO either of them, so dropping them cascades nowhere.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data. ``space_members``
--       is read and written through ``repositories/space_repo.py``
--       (``add_member`` / ``set_role`` — whose allow-list gains
--       ``moderator`` in the same change — / ``get_member`` /
--       ``list_members`` …); ``space_remote_members`` through
--       ``repositories/space_remote_member_repo.py`` (``add`` /
--       ``set_role`` / ``apply_member_event`` / ``list_admin_instances``
--       …). Every role predicate was classified (the PR's audit table):
--       settings guards stay role-EXACT on owner/admin
--       (``list_admin_instances`` is ``role='admin'``, so a moderator never
--       receives the delegated signing seed; ``apply_remote_admin_*``
--       drop any actor whose seat is not ``admin``); content guards read
--       ``CONTENT_AUTHORITY_ROLES``; writer gates read ``WRITER_ROLES`` so a
--       moderator keeps write access. The roster wire coerces an unknown
--       role DOWN to ``member`` (``mirrorable_remote_role``), so a pre-v_41
--       receiver of a ``moderator`` row stores ``member``, never a CHECK
--       violation.
--
--   (2) Non-migration alternative considered and rejected: an additive
--       ``content_moderator INTEGER NOT NULL DEFAULT 0`` flag column on both
--       tables (an ADD COLUMN, no rebuild). It admits illegal pairs
--       (``subscriber`` + moderator flag, ``owner`` + flag), splits one
--       seat's authority across two sources of truth that every reader —
--       and every federation payload — would have to combine, and it is the
--       per-user permission bitfield the domain model forbids
--       (``SpaceRole`` docstring). Reusing ``admin`` with a feature gate was
--       rejected because the whole point is a seat WITHOUT settings power.
--
--   (3) Smallest possible change. Two CHECKs gain one value each. No column
--       is added, renamed or dropped; no row is changed (an explicit-column
--       copy preserves every value); no default changes; no backfill. The
--       rebuild is destructive in SHAPE only because SQLite offers no
--       in-place CHECK edit — it is value-preserving in content.
--
-- ``foreign_keys`` is toggled off for the copy per the SQLite procedure
-- (both tables are children of ``spaces``). ``foreign_key_check`` is
-- informational under ``executescript`` — a developer tripwire when run by
-- hand, exactly as in 0042 / 0054.
PRAGMA foreign_keys=OFF;

-- ── space_members ────────────────────────────────────────────────────────
CREATE TABLE space_members_new (
    space_id              TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    user_id               TEXT NOT NULL,
    -- The one change: 'moderator' joins the vocabulary.
    role                  TEXT NOT NULL DEFAULT 'member'
                          CHECK(role IN ('owner','admin','moderator','member','subscriber')),
    joined_at             TEXT NOT NULL DEFAULT (datetime('now')),
    history_visible_from  TEXT,
    location_share_enabled INTEGER NOT NULL DEFAULT 0
                           CHECK(location_share_enabled IN (0,1)),
    space_display_name    TEXT,
    -- Per-space profile-picture override (§4.1.6). NULL means the
    -- member inherits their household picture. The bytes live in
    -- space_member_profile_pictures.
    picture_hash          TEXT,
    PRIMARY KEY (space_id, user_id)
);
INSERT INTO space_members_new
    (space_id, user_id, role, joined_at, history_visible_from,
     location_share_enabled, space_display_name, picture_hash)
SELECT
    space_id, user_id, role, joined_at, history_visible_from,
    location_share_enabled, space_display_name, picture_hash
FROM space_members;
DROP TABLE space_members;
ALTER TABLE space_members_new RENAME TO space_members;
-- DROP TABLE took the index with it; recreate it verbatim (0001).
CREATE INDEX IF NOT EXISTS idx_space_members_user ON space_members(user_id);

-- ── space_remote_members ─────────────────────────────────────────────────
CREATE TABLE space_remote_members_new (
    space_id       TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    instance_id    TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    user_pk        TEXT,
    display_name   TEXT,
    joined_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- The one change: 'moderator' joins the vocabulary. 'owner' stays out —
    -- ownership is a local-only privilege that cannot cross households.
    role           TEXT NOT NULL DEFAULT 'member'
                   CHECK(role IN ('member', 'admin', 'moderator', 'subscriber')),
    member_version INTEGER NOT NULL DEFAULT 0,
    tombstoned     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (space_id, instance_id, user_id)
);
INSERT INTO space_remote_members_new
    (space_id, instance_id, user_id, user_pk, display_name, joined_at,
     role, member_version, tombstoned)
SELECT
    space_id, instance_id, user_id, user_pk, display_name, joined_at,
    role, member_version, tombstoned
FROM space_remote_members;
DROP TABLE space_remote_members;
ALTER TABLE space_remote_members_new RENAME TO space_remote_members;
-- DROP TABLE took the index with it; recreate it verbatim (0001).
CREATE INDEX IF NOT EXISTS idx_space_remote_members_instance_user
    ON space_remote_members(instance_id, user_id);

PRAGMA foreign_key_check;
PRAGMA foreign_keys=ON;

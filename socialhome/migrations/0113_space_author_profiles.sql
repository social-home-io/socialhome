-- 0113 — a GFS follower of a space keeps its authors' public profiles.
-- Follows 0112 (moment public follow profile).
--
-- A household that follows a public / global space through a GFS holds no
-- roster for it, so every post there read "Someone". From v_60 an author's
-- household publishes the author's profile (name, bio, a ≤ 24 KB picture)
-- as an ``author_profile`` space item, signed by the author and encrypted
-- under the space content key. The follower verifies it and keeps the
-- newest one per (space, author) here.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited the tables that already hold people of a space:
--       ``space_members`` (local seats), ``space_remote_members`` (remote
--       seats — read by 17 modules for authorization: who may post, admin
--       forwarding, fan-out targets, bans), ``space_member_profile_pictures``
--       (a member's per-space picture override, exported by the §25.6 sync
--       ``members`` / ``member_pictures`` exporters to other members) and
--       ``remote_users`` (paired households' users only). Readers of the
--       new table: ``PersonSummaryService`` (names for the SPA),
--       ``GET /api/spaces/{id}/authors`` and its picture route, and the
--       ``author_profile`` item handler that writes it. Backup / export /
--       recovery copy it whole.
--   (2) Alternatives rejected: a ``space_remote_members`` row per author
--       (a seat is authorization — a follower inventing seats from relayed
--       items would make strangers look like members and could put their
--       households in fan-out sets); ``space_member_profile_pictures`` for
--       the bytes (the sync exporters would then hand a follower-side copy
--       to other members as if it were the member's own override);
--       ``remote_users`` (keyed by a paired household, and every household
--       surface would start treating a stranger as a known person); keeping
--       profiles in memory (every restart would read "Someone" until the
--       author's next profile, which may be an epoch away).
--   (3) Smallest change: one new table with a cascade on the space, no
--       change to any existing row or column.
CREATE TABLE IF NOT EXISTS space_author_profiles (
    space_id        TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    author_user_id  TEXT NOT NULL,
    -- The author household's identity key the item was verified against.
    author_pk       TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    bio             TEXT,
    picture_webp    BLOB,
    picture_hash    TEXT,
    profile_rev     TEXT NOT NULL,
    -- The author's signed stamp (tz-aware ISO 8601) — the newest wins.
    ts              TEXT NOT NULL,
    PRIMARY KEY (space_id, author_user_id)
);

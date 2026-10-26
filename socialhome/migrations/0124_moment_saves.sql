-- 0124 — save someone's moment (owner decision 6b, sign-offs 3 + 5,
-- 2026-10-25).
--
-- A person keeps another person's moment past its ``expires_at`` by saving
-- it. One row per (person, moment). While any save of a moment exists the
-- retention prune and its media sweep skip that row (``moment_repo``), so
-- the text and the file stay; the saver alone can still open it after
-- expiry (``get_readable``), block and protection filters still apply.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that reads or writes ``moments`` retention and
--       readability: ``SqliteMomentRepo.prune_expired`` /
--       ``list_expired_media_urls`` (``_PRUNABLE_SQL``), ``get_readable`` /
--       ``list_replies`` / ``count_engagement_for`` (``_READABLE_SQL``,
--       ``_THREAD_READABLE_SQL``), ``_tombstone_rows`` (every delete: the
--       author's via the mesh, a GFS delete certificate, a local admin,
--       ``USER_REMOVED``), the own archive (``_OWN_ARCHIVED_SQL``,
--       ``clear_media``, ``MomentArchiveService``), ``user_follows`` and
--       ``moment_public_follows`` deletes (``UserService.unfollow``,
--       ``MomentPublicService.unfollow``, the GFS-connection cascade) and
--       ``PeerUnpairService.purge``. No existing column or table says "this
--       person keeps this moment".
--   (2) Alternatives rejected:
--       * ``saved_posts`` — keyed on feed post ids with no reference to
--         ``moments``, so a moment's delete or prune would not cascade and
--         the prune predicate would have to join a table of another pillar.
--       * a household-wide ``kept`` column on ``moments`` — saves are per
--         person: after expiry Bob would see what only Alice saved, and
--         Alice's unsave could not tell whether Bob still keeps it.
--       * ``received_via = 'saved'`` — ``received_via`` has a CHECK
--         constraint (a table rebuild of ``moments``) and is the wrong axis
--         (how a row arrived, which the save gates and the unpair drop
--         still need).
--   (3) Smallest change: one new table (two foreign keys, both
--       ``ON DELETE CASCADE`` — a person's account removal or a moment's
--       prune drops its saves), one index for the per-moment lookups
--       (prune / media predicate, the tombstone trigger), and three
--       triggers. Nothing in ``moments`` changes shape and no row is
--       rewritten.
--
-- The invariants, in the database (like 0098 / 0100 / 0123 guard theirs):
--   * a deleted moment is never kept: a tombstone (``deleted_at`` set) drops
--     every save of it in the same transaction, whichever path deleted it;
--   * an unfollow drops the person's saves of that author's moments once no
--     follow of that author is left (a household follow or one through a
--     GFS — removing a GFS connection cascades its follows, and so its
--     saves). A save of a moment the person never followed is untouched by
--     follows.
-- The unpair drop (moments that came from the unpaired household,
-- ``received_via = 'household'`` and ``origin_instance_id`` = that
-- household) is in ``PeerUnpairService``: an unpair tombstone keeps its
-- ``remote_instances`` row and a re-pair replaces the row, so a trigger on
-- that table would fire at the wrong moments.

CREATE TABLE IF NOT EXISTS moment_saves (
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    moment_id  TEXT NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
    -- UTC, ISO 8601 with offset (``datetime.now(UTC).isoformat()``), like
    -- ``moments.created_at``; the Saved list pages on it, newest first.
    saved_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, moment_id)
);

CREATE INDEX IF NOT EXISTS idx_moment_saves_moment ON moment_saves(moment_id);

CREATE TRIGGER IF NOT EXISTS trg_moment_saves_drop_on_tombstone
AFTER UPDATE OF deleted_at ON moments
WHEN NEW.deleted_at IS NOT NULL
BEGIN
    DELETE FROM moment_saves WHERE moment_id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_moment_saves_drop_on_unfollow
AFTER DELETE ON user_follows
WHEN NOT EXISTS (
    SELECT 1 FROM moment_public_follows
     WHERE follower_user_id = OLD.follower_user_id
       AND followed_user_id = OLD.followed_user_id
)
BEGIN
    DELETE FROM moment_saves
     WHERE user_id = OLD.follower_user_id
       AND moment_id IN (
           SELECT id FROM moments WHERE author_user_id = OLD.followed_user_id
       );
END;

CREATE TRIGGER IF NOT EXISTS trg_moment_saves_drop_on_gfs_unfollow
AFTER DELETE ON moment_public_follows
WHEN NOT EXISTS (
    SELECT 1 FROM user_follows
     WHERE follower_user_id = OLD.follower_user_id
       AND followed_user_id = OLD.followed_user_id
)
 AND NOT EXISTS (
    SELECT 1 FROM moment_public_follows
     WHERE follower_user_id = OLD.follower_user_id
       AND followed_user_id = OLD.followed_user_id
)
BEGIN
    DELETE FROM moment_saves
     WHERE user_id = OLD.follower_user_id
       AND moment_id IN (
           SELECT id FROM moments WHERE author_user_id = OLD.followed_user_id
       );
END;

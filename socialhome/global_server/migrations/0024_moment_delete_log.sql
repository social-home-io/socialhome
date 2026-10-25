-- GFS 0024 — signed delete certificates of public moments, kept 30 days
-- (v_66). Follows 0023 (subscriber reads space_item).
--
-- A public moment may now live 30 days at a follower (v_65), but its
-- delete reaches a follower household over the GFS only live, or from the
-- 24 h offline queue: a household offline longer keeps showing a moment
-- its author deleted. Owner sign-off 4 (2026-10-25): the GFS keeps the
-- signed delete certificate — author, moment id, time, the author
-- household's origin signature; no content — for 30 days and hands it to a
-- follower household when it reconnects.
--
-- CLAUDE.md "audit before a migration":
-- 1. Audited every store and path that carries a delete here: the sealed
--    delete fan-out (``/gfs/moments/delete`` → ``MomentPublicRegistry
--    ._fan_out``: live sockets + the shared ``gfs_envelope_queue``, 24 h,
--    deleted on delivery, only for households seen within 24 h), the
--    ``SeenPayloadCache`` (in memory, 25 h), ``gfs_moment_key_mailbox`` (one
--    sealed key per author × household, replaced in place) and
--    ``gfs_moment_media`` (blobs, withdrawn on delete). None keeps a delete
--    past a day, and none can be read by a household that reconnects later.
-- 2. Alternatives rejected: raising the envelope queue TTL to 30 days (it
--    queues per recipient household — N copies of every delete AND every
--    moment, and its rows are sealed bytes the follower must be online
--    within 25 h to accept, v_64); asking the author's household on every
--    follower reconnect (the author must be online, and every reconnect of
--    every follower becomes an author round-trip — N × M work); keeping the
--    certificates in memory (a restart or another cluster node would forget
--    them); a column on ``gfs_moment_follows`` (a delete is per moment, not
--    per follow).
-- 3. Smallest change: one table, one row per (author, moment) however many
--    followers — INSERT OR IGNORE, so a replay changes nothing — cascading
--    with the author's registration, pruned 30 days after it was written
--    (maintenance sweep). ~0.35 KB a row.
CREATE TABLE IF NOT EXISTS gfs_moment_delete_log (
    author_user_id  TEXT NOT NULL
        REFERENCES gfs_user_registrations(user_id) ON DELETE CASCADE,
    moment_id       TEXT NOT NULL,
    -- The certificate as canonical JSON (``moment_origin.DELETE_CERT_FIELDS``)
    -- — checked against the author household's key before it was kept.
    cert            TEXT NOT NULL,
    -- Unix seconds this server took it; the 30-day life runs from here.
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (author_user_id, moment_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_moment_delete_log_author
    ON gfs_moment_delete_log(author_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_gfs_moment_delete_log_created
    ON gfs_moment_delete_log(created_at);

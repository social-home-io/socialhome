-- GFS 0021 — public moments are encrypted from the GFS (v_62): what the
-- GFS needs to route sealed moment keys. Follows 0020 (global space place
-- radius).
--
-- A public moment now reaches followers as ciphertext under its author's
-- moment key, and the key reaches each follower household sealed to that
-- household's key-wrap key (the GFS can't open it). Two gaps without a
-- store: a follower household offline when its sealed key went out never
-- got it (WebSocket-only delivery has no retry), and an author can't tell
-- which follower households can open encrypted moments at all.
--
-- CLAUDE.md "audit before a migration":
-- 1. Audited every path that touches follows and household delivery:
--    ``gfs_moment_follows`` (``add_follow`` / ``remove_follow`` /
--    ``followers_of`` — the fan-out), ``client_instances`` (the household's
--    registered identity + key-wrap key, read for ``follow_changed``), the
--    §D2b ``gfs_envelope_queue`` (sealed household-to-household envelopes
--    queued while the recipient is offline, deleted on delivery) and the
--    Phase-5b subscriber key handoff (re-asks the space owner on every
--    subscriber reconnect; stores nothing).
-- 2. Alternatives rejected: the §D2b envelope queue (it deletes a row once
--    delivered, so a follower whose import failed has nothing to re-pull,
--    and it is capped per recipient for a different purpose); asking the
--    author's household again on every follower reconnect, as the space
--    handoff does (the author must be online at that moment, and every
--    reconnect of every follower becomes an author round-trip — N × M
--    work for a popular author); keeping the suites in memory (a restart
--    would make every follower look unable to decrypt).
-- 3. Smallest change: one NULL-defaulted column on the follow (no backfill
--    — a follow made before v_62 has no declared suites, which is exactly
--    "can't open encrypted moments yet") and one table holding at most the
--    LATEST sealed key per (author, follower household), replaced in place,
--    cascading with the registration and the household.

-- What the follower household said it can open, as JSON
-- ``{"kem": [...], "seal": [...]}`` (signed in its follow request).
ALTER TABLE gfs_moment_follows ADD COLUMN moment_suites TEXT;

CREATE TABLE IF NOT EXISTS gfs_moment_key_mailbox (
    author_user_id       TEXT NOT NULL REFERENCES gfs_user_registrations(user_id) ON DELETE CASCADE,
    follower_instance_id TEXT NOT NULL REFERENCES client_instances(instance_id) ON DELETE CASCADE,
    -- The routing key id of the sealed key (plaintext on the delivery).
    key_gen              INTEGER NOT NULL,
    key_step             INTEGER NOT NULL,
    -- The author-signed delivery as JSON: ciphertext sealed to the
    -- follower's key-wrap key — this server cannot open it.
    delivery             TEXT NOT NULL,
    -- 1 once pushed over a live socket; a reconnect re-sends only 0 rows.
    delivered            INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0, 1)),
    updated_at           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (author_user_id, follower_instance_id)
);
CREATE INDEX IF NOT EXISTS idx_gfs_moment_key_mailbox_follower
    ON gfs_moment_key_mailbox(follower_instance_id, delivered);

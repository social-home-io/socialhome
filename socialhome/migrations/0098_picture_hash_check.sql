-- 0098 — picture hashes are 16 lowercase hex, enforced by the database.
-- Follows 0097 (household zones).
--
-- ``users`` / ``remote_users`` / ``space_members`` ``picture_hash`` is
-- interpolated into cache-busting picture URLs (``…/picture?v=<hash>``).
-- Every hash this household computes is ``compute_picture_hash`` = the
-- 16-char lowercase SHA-256 hex prefix. Before #908, a paired peer whose
-- payload shipped no (or rejected) picture bytes could store ANY string
-- there (``ab&sig=evil``, ``#x``, megabytes) — #908 validates at the
-- federation boundary, but rows written before it stay until the peer
-- publishes again. This migration (a) repairs them in one pass — a value
-- that is not exactly 16 lowercase hex becomes NULL (= initials until the
-- next real sync) — and (b) installs BEFORE INSERT / BEFORE UPDATE triggers
-- that refuse such a value from any writer (today's code, a regression, a
-- restored backup). The repair runs FIRST, so no install boot-loops.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every writer: ``user_repo.set_picture_hash`` /
--       ``set_remote_picture_hash`` / ``upsert_remote``,
--       ``space_repo.set_member_profile`` / ``save_member``, the federation
--       inbound handlers + space sync receiver (cleaned by #908), the local
--       picture upload / clear paths (``compute_picture_hash`` / NULL), and
--       ``backup_service._import_table`` (restore now cleans the column
--       first so an old backup's bad hash never drops the whole row). No
--       writer stores ``''`` as "no picture" — they all use NULL.
--   (2) Non-migration alternatives considered and rejected:
--       * A read-side guard in every URL builder — the bad rows stay on
--         disk forever and every future builder must remember the guard.
--       * Waiting for the peer to publish again — a hostile peer won't.
--       * A CHECK constraint — SQLite can't add one without rebuilding
--         three hot tables; a trigger gives the same guarantee for every
--         writer with no rebuild.
--   (3) Smallest change: an UPDATE that touches only invalid values (NULL
--       stays NULL, valid stays valid) plus six triggers. No schema change.
--       A future migration that rebuilds one of these tables must
--       recreate its two triggers.

UPDATE users SET picture_hash = NULL
 WHERE picture_hash IS NOT NULL AND (typeof(picture_hash) <> 'text' OR length(picture_hash) <> 16 OR picture_hash GLOB '*[^0-9a-f]*');

DROP TRIGGER IF EXISTS users_picture_hash_insert_check;
CREATE TRIGGER users_picture_hash_insert_check
BEFORE INSERT ON users
WHEN NEW.picture_hash IS NOT NULL AND (typeof(NEW.picture_hash) <> 'text' OR length(NEW.picture_hash) <> 16 OR NEW.picture_hash GLOB '*[^0-9a-f]*')
BEGIN
    SELECT RAISE(ABORT, 'picture_hash must be 16 lowercase hex characters');
END;

DROP TRIGGER IF EXISTS users_picture_hash_update_check;
CREATE TRIGGER users_picture_hash_update_check
BEFORE UPDATE OF picture_hash ON users
WHEN NEW.picture_hash IS NOT NULL AND (typeof(NEW.picture_hash) <> 'text' OR length(NEW.picture_hash) <> 16 OR NEW.picture_hash GLOB '*[^0-9a-f]*')
BEGIN
    SELECT RAISE(ABORT, 'picture_hash must be 16 lowercase hex characters');
END;

UPDATE remote_users SET picture_hash = NULL
 WHERE picture_hash IS NOT NULL AND (typeof(picture_hash) <> 'text' OR length(picture_hash) <> 16 OR picture_hash GLOB '*[^0-9a-f]*');

DROP TRIGGER IF EXISTS remote_users_picture_hash_insert_check;
CREATE TRIGGER remote_users_picture_hash_insert_check
BEFORE INSERT ON remote_users
WHEN NEW.picture_hash IS NOT NULL AND (typeof(NEW.picture_hash) <> 'text' OR length(NEW.picture_hash) <> 16 OR NEW.picture_hash GLOB '*[^0-9a-f]*')
BEGIN
    SELECT RAISE(ABORT, 'picture_hash must be 16 lowercase hex characters');
END;

DROP TRIGGER IF EXISTS remote_users_picture_hash_update_check;
CREATE TRIGGER remote_users_picture_hash_update_check
BEFORE UPDATE OF picture_hash ON remote_users
WHEN NEW.picture_hash IS NOT NULL AND (typeof(NEW.picture_hash) <> 'text' OR length(NEW.picture_hash) <> 16 OR NEW.picture_hash GLOB '*[^0-9a-f]*')
BEGIN
    SELECT RAISE(ABORT, 'picture_hash must be 16 lowercase hex characters');
END;

UPDATE space_members SET picture_hash = NULL
 WHERE picture_hash IS NOT NULL AND (typeof(picture_hash) <> 'text' OR length(picture_hash) <> 16 OR picture_hash GLOB '*[^0-9a-f]*');

DROP TRIGGER IF EXISTS space_members_picture_hash_insert_check;
CREATE TRIGGER space_members_picture_hash_insert_check
BEFORE INSERT ON space_members
WHEN NEW.picture_hash IS NOT NULL AND (typeof(NEW.picture_hash) <> 'text' OR length(NEW.picture_hash) <> 16 OR NEW.picture_hash GLOB '*[^0-9a-f]*')
BEGIN
    SELECT RAISE(ABORT, 'picture_hash must be 16 lowercase hex characters');
END;

DROP TRIGGER IF EXISTS space_members_picture_hash_update_check;
CREATE TRIGGER space_members_picture_hash_update_check
BEFORE UPDATE OF picture_hash ON space_members
WHEN NEW.picture_hash IS NOT NULL AND (typeof(NEW.picture_hash) <> 'text' OR length(NEW.picture_hash) <> 16 OR NEW.picture_hash GLOB '*[^0-9a-f]*')
BEGIN
    SELECT RAISE(ABORT, 'picture_hash must be 16 lowercase hex characters');
END;

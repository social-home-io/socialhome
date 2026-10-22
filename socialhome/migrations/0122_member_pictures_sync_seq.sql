-- 0122 — §25.6 incremental space sync covers ``member_pictures``.
--
-- ``space_member_profile_pictures.sync_seq`` (NULL default) — the same
-- per-household change stamp migrations 0086 / 0088 put on the other synced
-- tables, set by an ``AFTER INSERT`` trigger and by an ``AFTER UPDATE``
-- trigger that fires only when the picture itself changes (``hash``, the
-- bytes, the size) or the stamp is cleared — never on ``updated_at`` alone,
-- so a receiver re-applying a picture it already holds stamps nothing. The
-- ``member_pictures`` exporter then streams, in a periodic session, only the
-- pictures changed since the requester's confirmed watermark instead of every
-- picture on every session (~50 KB each in the chunk, re-encoded on arrival).
-- ``SYNC_SHAPE_VERSION`` 5 makes every household's next session a full one,
-- so a picture changed before the upgrade (unstamped) still streams once.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every code path that touches this table: its writes are
--       ``SqliteProfilePictureRepo.set_member_picture`` (an ``INSERT … ON
--       CONFLICT DO UPDATE`` upsert — never ``INSERT OR REPLACE``, so an
--       idempotent apply is an UPDATE that the trigger's WHEN ignores) and
--       ``clear_member_picture`` (a DELETE: the removal reaches peers as the
--       member row's ``picture_hash`` → NULL and the live
--       ``SPACE_MEMBER_PROFILE_UPDATED``, not as a picture record); its
--       readers are ``get_member_picture`` / ``list_member_picture_user_ids``
--       (routes, ``SpaceService``), the ``member_pictures`` exporter and the
--       §25.6 receiver (which writes through ``set_member_picture``);
--       migrations 0099 (a picture-hash repair that only reads it) and
--       0113 (author profiles, which read it). The 0065 rebuild touched
--       ``space_members``, not this table. The backup export does not
--       include the table (``backup_service.EXPORTABLE_TABLES``): a restored
--       household gets the pictures back from its peers' full streams.
--   (2) Alternatives rejected: ``updated_at`` is a wall-clock stamp that an
--       idempotent re-apply rewrites, so it can't tell a changed picture
--       from a re-sent one; comparing hashes per session would need the
--       requester's whole picture list on every BEGIN (bytes the stamp
--       avoids); keeping the resource "full" is today's cost.
--   (3) Smallest change: one nullable ``ADD COLUMN`` (no backfill — the
--       shape bump streams each household in full once) and the two
--       triggers the other covered tables have. No index: a space has a
--       handful of picture rows, served by the primary key's space prefix.

ALTER TABLE space_member_profile_pictures ADD COLUMN sync_seq INTEGER;

CREATE TRIGGER space_member_profile_pictures_sync_seq_insert
AFTER INSERT ON space_member_profile_pictures
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_member_profile_pictures
       SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE space_id = NEW.space_id AND user_id = NEW.user_id;
END;

CREATE TRIGGER space_member_profile_pictures_sync_seq_update
AFTER UPDATE ON space_member_profile_pictures
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.space_id, NEW.user_id, NEW.hash, NEW.bytes_webp, NEW.width, NEW.height)
   IS NOT (OLD.space_id, OLD.user_id, OLD.hash, OLD.bytes_webp, OLD.width, OLD.height))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_member_profile_pictures
       SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE space_id = NEW.space_id AND user_id = NEW.user_id;
END;

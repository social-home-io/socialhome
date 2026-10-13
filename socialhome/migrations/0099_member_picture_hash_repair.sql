-- 0099 — a seat points at the per-space picture blob this household holds.
-- Follows 0098 (picture hash check).
--
-- ``space_members.picture_hash`` says which per-space picture a seat shows;
-- the bytes live in ``space_member_profile_pictures``. Two older paths left
-- a seat NULL while its blob survived:
--   * before #910 every §25.6 ``members`` upsert (and a re-seat) wrote NULL
--     over the hash — for remote members AND this household's own people;
--   * 0098 NULLed hashes that were not 16 lowercase hex.
-- The seat then says "no picture" while the household holds one. From v_58
-- a provider's ``members`` record with ``picture_hash: null`` is a reset its
-- co-members act on (they drop their copy), and ``member_pictures`` skips a
-- seat with no hash — so the damage would spread instead of heal. This
-- re-points each such seat at its blob's hash.
--
-- LOCAL members only (``user_id`` in ``users`` — the join
-- ``list_local_member_user_ids`` uses). Before this release the live
-- ``SPACE_MEMBER_PROFILE_UPDATED`` reset NULLed a REMOTE member's seat but
-- kept the blob: the same "seat NULL + blob" shape, meaning "removed". A
-- remote seat is ambiguous here, so it is left to sync: the ``members``
-- exporter omits the key for it (holds a blob), and the member's own
-- household settles it — its ``member_pictures`` re-points the seat, or its
-- v_58 ``null`` drops the blob. Our own people are only ever authored here,
-- and our local reset has always deleted the blob, so for them the shape can
-- only be wipe damage.
--
-- Only a hash 0098's triggers accept (16 lowercase hex, text) is copied: any
-- other value would abort the UPDATE and boot-loop the install. A blob whose
-- hash is not valid keeps its NULL seat; the ``members`` exporter then omits
-- the key (it holds bytes), so no co-member reads it as a reset.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every writer of both tables: ``space_repo.save_member`` (the
--       pre-#910 wipe; COALESCE since), ``set_member_profile`` (local edit,
--       live ``SPACE_MEMBER_PROFILE_UPDATED``, the §25.6 ``member_pictures``
--       and v_58 ``members`` reset paths), ``profile_picture_repo
--       .set_member_picture`` / ``clear_member_picture`` (local upload /
--       reset, live inbound, sync), 0098's repair, and backup restore. From
--       this release every reset path deletes the blob too, so going forward
--       no NEW "seat NULL + blob present" row is written; rows already on
--       disk remain for remote members (settled by sync, see above) and for
--       a local blob with an invalid hash.
--   (2) Non-migration alternatives considered and rejected:
--       * Computing the hash at export time from the blob — the provider's
--         own SPA would still render initials (it reads the seat column),
--         and every reader would have to remember the fallback.
--       * Re-hashing the bytes at startup in Python — the stored blob hash
--         IS our ``compute_picture_hash`` of those bytes (every writer
--         stores it that way since #908/#910); copying it is equivalent.
--       * Leaving it — a v_58 co-member would delete a correct picture.
--   (3) Smallest change: one UPDATE that touches only LOCAL members' NULL
--       seats with a valid blob hash. No schema change, no new trigger.

UPDATE space_members
   SET picture_hash = (
       SELECT p.hash FROM space_member_profile_pictures p
        WHERE p.space_id = space_members.space_id
          AND p.user_id = space_members.user_id
   )
 WHERE picture_hash IS NULL
   AND EXISTS (
       SELECT 1 FROM space_member_profile_pictures p
        WHERE p.space_id = space_members.space_id
          AND p.user_id = space_members.user_id
          AND typeof(p.hash) = 'text'
          AND length(p.hash) = 16
          AND p.hash NOT GLOB '*[^0-9a-f]*'
   )
   AND user_id IN (SELECT user_id FROM users);

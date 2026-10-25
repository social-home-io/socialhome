-- 0123 — a reply never goes further than the moment it answers.
--
-- Before #1158 the composer pre-ticked "share publicly" on a reply and the
-- service stored ``is_public`` as sent, so a reply to a household moment
-- could be stored ``is_public = 1``. The service now stores such a reply as
-- not public, and the outbound and the share link refuse it; this clears the
-- rows the old behaviour left behind, so nothing that reads ``is_public``
-- (the share-link button, a share link, the reply turning top-level when
-- its root is deleted and ``parent_moment_id`` goes NULL) treats them as
-- public. Only this household's own replies (``received_via = 'self'``) —
-- a stranger's public reply a follower holds is not ours to publish.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that reads or writes ``moments.is_public``:
--       written by ``SqliteMomentRepo.save`` (create, and the GFS-inbound
--       ``is_public=True``) and the delete tombstone (``is_public=0``); read
--       by ``MomentPublicOutbound._on_created`` (now checks the parent),
--       ``MomentPublicShareService.link`` (now checks the parent),
--       ``list_public_for`` (no caller) and the API's ``is_public`` field
--       (the SPA's share-link button on a moment's page). The GFS delete
--       (``_on_deleted``) does not read it, so an author can still withdraw
--       a copy the old behaviour published.
--   (2) Alternatives rejected: read-path checks alone miss a reply whose
--       root was deleted — the tombstone nulls ``parent_moment_id``, so the
--       reply then reads as a public top-level moment and a share link
--       would put a fresh copy on a GFS; waiting for expiry (≤ 7 days)
--       leaves that window open.
--   (3) Smallest change: one UPDATE of the affected rows, no schema change,
--       no index (``idx_moments_parent`` serves the parent lookup).

UPDATE moments
   SET is_public = 0
 WHERE is_public = 1
   AND received_via = 'self'
   AND parent_moment_id IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM moments AS root
        WHERE root.id = moments.parent_moment_id
          AND root.is_public = 1
   );

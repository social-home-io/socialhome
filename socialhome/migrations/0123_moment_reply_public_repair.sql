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
--   (3) Smallest change: one UPDATE of the affected rows plus two
--       triggers; no new column, table or index (``idx_moments_parent``
--       and the primary key serve the parent lookups).
--
-- The guard (the invariant, in the database): one of this household's own
-- replies is public only while its parent is stored here and public — a
-- reply under a public parent stays allowed (owner decision 2026-10-25).
-- ``BEFORE INSERT`` / ``BEFORE UPDATE`` triggers refuse anything else, like
-- 0098 / 0100 / 0101 guard their columns. A CHECK constraint can't read
-- another row, and a table rebuild was not needed. Writers audited:
--   * ``MomentService.create`` stores a reply public only when its root is
--     stored and public (#1158) — the only writer of ``received_via =
--     'self'`` rows.
--   * GFS inbound (``received_via = 'gfs'``, ``is_public = 1``) and
--     household inbound (``'household'``) are not this household's own and
--     are stored as received, whatever we hold of their parent.
--   * The delete tombstone (``_tombstone_rows``) first detaches the replies
--     (``parent_moment_id`` → NULL), then clears the deleted row's
--     ``is_public``; the content-free tombstone insert is ``'household'``
--     with ``is_public = 0``. The retention prune's ``ON DELETE SET NULL``
--     detaches too. None of these can be refused.
--   * Backup restore does not carry ``moments`` (not in
--     ``EXPORTABLE_TABLES``).
-- The update guard fires only on ``is_public`` / ``parent_moment_id`` /
-- ``received_via`` writes. It does not guard the parent side: an inbound
-- upsert may store a moment we hold from a GFS as not public, and refusing
-- it would drop a legitimate write — the outbound and the share link
-- re-check the parent at read time for that case.

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

CREATE TRIGGER IF NOT EXISTS trg_moments_reply_public_insert
BEFORE INSERT ON moments
WHEN NEW.received_via = 'self'
 AND NEW.parent_moment_id IS NOT NULL
 AND NEW.is_public = 1
 AND NOT EXISTS (
     SELECT 1 FROM moments AS parent
      WHERE parent.id = NEW.parent_moment_id
        AND parent.is_public = 1
 )
BEGIN
    SELECT RAISE(ABORT, 'a reply can be public only under a public moment');
END;

CREATE TRIGGER IF NOT EXISTS trg_moments_reply_public_update
BEFORE UPDATE OF is_public, parent_moment_id, received_via ON moments
WHEN NEW.received_via = 'self'
 AND NEW.parent_moment_id IS NOT NULL
 AND NEW.is_public = 1
 AND NOT EXISTS (
     SELECT 1 FROM moments AS parent
      WHERE parent.id = NEW.parent_moment_id
        AND parent.is_public = 1
 )
BEGIN
    SELECT RAISE(ABORT, 'a reply can be public only under a public moment');
END;

-- 0148 — one pending admin elevation per (space, household, user id).
--
-- An admin invite link seats the redeemer as a member and files a pending
-- ``space_join_requests`` row with ``requested_role='admin'`` for the owner
-- to approve. Since #1386 that request is keyed by (space, household, user
-- id) — but only by a read-then-insert in
-- ``SpaceService._file_admin_elevation``, so two redeems of one household
-- arriving together both read "nothing on file" and both insert: the owner
-- sees the same request twice, and approving one leaves a stale twin.
--
-- 1. Repair: of the pending elevations sharing one key, the OLDEST stays
--    pending (``requested_at``, then ``id`` — deterministic); the later ones
--    become ``expired`` (nobody decided on them, and the row stays so a bell
--    that names its id still resolves). ``reviewed_by`` stays NULL.
-- 2. Constraint: a partial UNIQUE index on exactly the columns the code keys
--    on. A local member's row has no household (NULL), and SQLite treats
--    NULLs as distinct in a UNIQUE index, so the household is indexed as
--    ``IFNULL(…, '')`` (an instance id is never empty).
--    ``SqliteSpaceRepo.save_join_request`` inserts with ``ON CONFLICT DO
--    NOTHING`` and returns the row already on file.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every code path touching this data. ``space_join_requests``
--       (0001; ``requested_role`` 0055; rebuilt in 0111; ``gfs_relay`` 0143)
--       has only the ``id`` primary key and two plain indexes. All SQL is in
--       ``repositories/space_repo.py``: ``save_join_request`` is the one
--       INSERT; ``update_join_request_status`` / ``answer_join_request`` are
--       the only status writes and no caller ever moves a row BACK to
--       ``pending``, so an UPDATE cannot hit the index;
--       ``set_join_request_gfs_relay`` touches no key column. Rows with
--       ``requested_role`` are written by
--       ``SpaceService._file_admin_elevation`` alone (local
--       ``accept_invite_token``, and the §D2 / §D2b redeem through
--       ``file_admin_elevation_request``), always on the household that
--       seated the redeemer — the applicant's own copies
--       (``request_join_remote``, the GFS join-request coordinator,
--       ``_record_review_request``, the private-invite handler) and the
--       host's ordinary join requests (``file_remote_join_request``,
--       ``local_community``) never carry a role and are outside the index.
--       Readers address a request by ``id`` (approve / deny / withdraw /
--       expire, the bell, the SPA list).
--   (2) Non-migration alternatives considered and rejected:
--       * The per-space seating lock around the read-then-insert — an
--         in-process lock is not an invariant of the data (a restore, a
--         future caller that forgets it), and it repairs nothing on disk.
--       * De-duplicating at read time (list / approve) — every reader would
--         have to agree on the survivor, and the twin rows stay.
--       * A deterministic request id (a hash of the key) on the existing
--         primary key — a household could never file again after its first
--         request was denied or expired.
--   (3) Smallest change: one UPDATE that touches only surplus pending
--       elevation rows (none on a database that never raced) and one partial
--       index over the pending elevations only — no new column, no table
--       rebuild, ordinary join requests untouched.
UPDATE space_join_requests
   SET status = 'expired',
       reviewed_at = datetime('now')
 WHERE status = 'pending'
   AND requested_role = 'admin'
   AND EXISTS (
        SELECT 1 FROM space_join_requests AS older
         WHERE older.status = 'pending'
           AND older.requested_role = 'admin'
           AND older.space_id = space_join_requests.space_id
           AND older.user_id = space_join_requests.user_id
           AND IFNULL(older.remote_applicant_instance_id, '')
               = IFNULL(space_join_requests.remote_applicant_instance_id, '')
           AND (
                older.requested_at < space_join_requests.requested_at
                OR (
                    older.requested_at = space_join_requests.requested_at
                    AND older.id < space_join_requests.id
                )
           )
   );

CREATE UNIQUE INDEX IF NOT EXISTS idx_space_join_requests_one_pending_elevation
    ON space_join_requests(
        space_id, IFNULL(remote_applicant_instance_id, ''), user_id
    )
    WHERE status = 'pending' AND requested_role = 'admin';

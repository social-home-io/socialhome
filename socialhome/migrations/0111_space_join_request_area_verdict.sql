-- 0111 — a join request remembers a local community's verdict, and an
-- applicant household can keep its own pending request for a space it does
-- not hold.
-- Follows 0110 (peer space directory drop verdict).
--
-- Two changes to ``space_join_requests``, one rebuild:
--
-- * ``area_verdict`` — a join to a local community carries the joiner's home
--   rounded to ~1 km; the host decides on it and keeps ONLY the verdict
--   (owner decision 2026-10-19: the point is never stored). A request whose
--   ~1 km cell straddles the rim is filed for the admins whatever the join
--   mode, and they see "Near the edge" — this column is how they see it
--   (``'inside'`` / ``'edge'``; NULL for a space that is not a local
--   community).
--
-- * No foreign key on ``space_id``. An APPLICANT household keeps a row for
--   its own outgoing request (``request_join_remote``; ``remote_applicant_
--   instance_id`` names the HOST there) so the host's approval can be
--   matched back to the person and redeemed. The space lives on the host:
--   the applicant holds no ``spaces`` row for it, so the 0001 foreign key
--   refused every such row (``FOREIGN KEY constraint failed``) — a remote
--   "Ask to join" never got past the applicant's own database. The
--   ``ON DELETE CASCADE`` the key gave is kept by a trigger: deleting a
--   ``spaces`` row still deletes its requests.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path on the table: ``SqliteSpaceRepo.save_join_request``
--       / ``list_pending_join_requests`` / ``list_pending_join_request_
--       space_ids_for_user`` / ``update_join_request_status`` /
--       ``list_expired_join_requests`` / ``has_approved_join_request`` and
--       the two direct reads in ``SpaceService.approve_join_request`` /
--       ``deny_join_request`` / ``on_remote_join_request_approved``; the
--       writers are the local request, the inbound ``SPACE_JOIN_REQUEST``,
--       the admin-link elevation, the applicant-side ``request_join_remote``
--       and (new) a local community's edge review of a link redeem or an
--       invite accept. The backup / export / recovery dumps copy the table
--       whole and pick the new column up as is.
--   (2) Alternatives rejected for the verdict: keeping the point (the model
--       forbids it); re-asking the joiner when the admin opens the queue
--       (the joiner may be offline, and the point would travel twice);
--       reusing ``message`` (user text) or ``requested_role`` (its CHECK
--       allows only 'admin' and it means a seat role). For the foreign key:
--       a stub ``spaces`` row on the applicant (it would surface as a space
--       the person is not in, and the GFS-mirror / invitation code treats a
--       stub as a join); a separate outgoing-requests table (a second
--       request store for one concept, and every reader would need both).
--   (3) Smallest change that does both: SQLite cannot drop a foreign key in
--       place, so the table is rebuilt once with the same columns, CHECKs
--       and indexes plus one NULL-defaulted column; no backfill (an existing
--       request was filed under the old rule and shows no chip).
PRAGMA foreign_keys=OFF;

CREATE TABLE space_join_requests_new (
    id                           TEXT PRIMARY KEY,
    space_id                     TEXT NOT NULL,
    remote_applicant_instance_id TEXT,
    remote_applicant_pk          TEXT,
    user_id                      TEXT NOT NULL,
    message                      TEXT,
    status                       TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','denied','expired','withdrawn')),
    requested_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_by                  TEXT,
    reviewed_at                  TEXT,
    expires_at                   TEXT NOT NULL,
    requested_role               TEXT
        CHECK(requested_role IS NULL OR requested_role IN ('admin')),
    area_verdict                 TEXT
        CHECK(area_verdict IS NULL OR area_verdict IN ('inside', 'edge'))
);
INSERT INTO space_join_requests_new
    (id, space_id, remote_applicant_instance_id, remote_applicant_pk, user_id,
     message, status, requested_at, reviewed_by, reviewed_at, expires_at,
     requested_role)
SELECT
    id, space_id, remote_applicant_instance_id, remote_applicant_pk, user_id,
    message, status, requested_at, reviewed_by, reviewed_at, expires_at,
    requested_role
FROM space_join_requests;
DROP TABLE space_join_requests;
ALTER TABLE space_join_requests_new RENAME TO space_join_requests;
-- DROP TABLE took the indexes with it; recreate them verbatim (0001).
CREATE INDEX IF NOT EXISTS idx_space_join_requests_pending
    ON space_join_requests(space_id, status, requested_at);
CREATE INDEX IF NOT EXISTS idx_space_join_requests_expires
    ON space_join_requests(expires_at) WHERE status='pending';

-- The cascade the foreign key used to give.
CREATE TRIGGER IF NOT EXISTS space_join_requests_follow_space_delete
    AFTER DELETE ON spaces
BEGIN
    DELETE FROM space_join_requests WHERE space_id = OLD.id;
END;

PRAGMA foreign_key_check;
PRAGMA foreign_keys=ON;

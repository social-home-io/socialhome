-- 0102 — a follower neither shares nor keeps the members' whereabouts.
-- Follows 0101 (household space members).
--
-- Owner decision 13: a follower (``subscriber`` seat) reads a space's posts
-- but is an outsider to its *people*. #1000 / #1006 enforce that from now
-- on (reads, live frames, federation fan-out, receive-side guards). Two kinds
-- of rows written before them stay on disk until repaired here, once:
--
--   (a) ``space_members.location_share_enabled = 1`` on a follower's seat —
--       ``PATCH …/members/me/location-sharing`` took it from any seat, so a
--       follower's GPS pin was fanned to the members. The flag goes to 0.
--   (b) On a household whose local users hold no member seat (owner /
--       admin / moderator / member) in a space another household hosts — a
--       follower-only household, or one with no seat left — the members'
--       map pins (``space_remote_member_locations``), the zones
--       (``space_zones``, live rows and tombstones) and the admin proposal
--       mirror (``space_admin_proposals``; its votes go with it, explicitly
--       and by ON DELETE CASCADE) are deleted. A space THIS household hosts
--       keeps everything (it is the source of truth); a household with any
--       local member seat keeps everything. "Local" = a ``users`` row, the
--       rule ``holds_member_seat`` / ``list_local_member_user_ids`` use.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path touching these rows: the location-sharing PATCH
--       and ``GET /api/me/space-location-sharing`` (``space_repo.
--       set_member_location_sharing`` / ``list_user_memberships_with_
--       location_feature`` / ``list_location_shared_spaces_for_user``),
--       ``space_repo.save_member`` (local seats, the §25.6 ``members``
--       receiver, backup restore), ``SpaceLocationOutbound`` (fan-out, opt-in
--       refire, mode switch), the inbound ``SPACE_LOCATION_UPDATED``
--       (private_invite_handler), ``SPACE_ZONE_UPSERTED`` / ``_DELETED``
--       (space_content), the §25.6 ``space_zones`` / ``space_zones_deleted``
--       receiver, the proposal mirror (``SpaceApprovalService.
--       apply_mirror_update``) and the read routes (``…/presence``,
--       ``…/zones``, ``…/proposals``, already member-only since #1000).
--   (2) Non-migration alternatives considered and rejected:
--       * Filtering at read time only — the follower-gated reads already
--         refuse, but the rows (GPS of other households' members, their
--         places, governance) would stay on disk forever, in every backup
--         and data export; owner rule: data already on disk is repaired.
--       * Waiting for the sender to overwrite — nothing is sent to a
--         follower-only household any more, so nothing ever would.
--       * A startup reconcile — the same once-only effect with a second
--         bookkeeping mechanism and no transaction around it.
--   (3) Smallest change: one UPDATE that touches only followers' flags set
--       to 1, four DELETEs scoped to non-hosted spaces without a local
--       member seat, plus three triggers. No new column or table.
--
-- The guard (an invariant the database can hold on its own row): a
-- follower's seat has sharing off. AFTER INSERT / AFTER UPDATE OF
-- ``role, location_share_enabled`` triggers set it back to 0 — coercing,
-- not refusing, because the §25.6 roster receiver and a backup restore
-- write whole member rows whose flag comes from elsewhere (an older host
-- may still send 1 for a follower): refusing would drop the whole roster
-- row, i.e. the seat. A member who becomes a follower loses the flag the
-- same way. A CHECK constraint was rejected: it needs a ``space_members``
-- rebuild (a hot table with many dependants) and would refuse those
-- writers too. The application refuses the opt-in up front (403
-- ``SUBSCRIBER_READ_ONLY``); the trigger only keeps any other writer
-- honest.
--
-- (b) gets no trigger: "this household holds a member seat" depends on
-- another table and changes over time (a member seat arriving after a zone
-- packet is the normal join race, see 0010), so a per-row trigger can't
-- state it without refusing legitimate first writes. The receive-side
-- guards (``holds_member_seat`` on the live events since #1006, and on the
-- §25.6 zone catch-up since this release) are the enforcement.

UPDATE space_members
   SET location_share_enabled = 0
 WHERE role = 'subscriber'
   AND location_share_enabled <> 0;

DROP TRIGGER IF EXISTS trg_space_members_follower_no_sharing_insert;
CREATE TRIGGER trg_space_members_follower_no_sharing_insert
AFTER INSERT ON space_members
WHEN NEW.role = 'subscriber' AND NEW.location_share_enabled <> 0
BEGIN
    UPDATE space_members SET location_share_enabled = 0
     WHERE space_id = NEW.space_id AND user_id = NEW.user_id;
END;

DROP TRIGGER IF EXISTS trg_space_members_follower_no_sharing_update;
CREATE TRIGGER trg_space_members_follower_no_sharing_update
AFTER UPDATE OF role, location_share_enabled ON space_members
WHEN NEW.role = 'subscriber' AND NEW.location_share_enabled <> 0
BEGIN
    UPDATE space_members SET location_share_enabled = 0
     WHERE space_id = NEW.space_id AND user_id = NEW.user_id;
END;

CREATE TEMP TABLE _follower_only_spaces AS
SELECT s.id AS space_id
  FROM spaces s
 WHERE s.owner_instance_id <> (SELECT instance_id FROM instance_identity)
   AND NOT EXISTS (
       SELECT 1 FROM space_members m
         JOIN users u ON u.user_id = m.user_id
        WHERE m.space_id = s.id
          AND m.role IN ('owner', 'admin', 'moderator', 'member')
   );

DELETE FROM space_remote_member_locations
 WHERE space_id IN (SELECT space_id FROM _follower_only_spaces);

DELETE FROM space_zones
 WHERE space_id IN (SELECT space_id FROM _follower_only_spaces);

DELETE FROM space_admin_proposal_votes
 WHERE proposal_id IN (
       SELECT id FROM space_admin_proposals
        WHERE space_id IN (SELECT space_id FROM _follower_only_spaces)
 );

DELETE FROM space_admin_proposals
 WHERE space_id IN (SELECT space_id FROM _follower_only_spaces);

DROP TABLE _follower_only_spaces;

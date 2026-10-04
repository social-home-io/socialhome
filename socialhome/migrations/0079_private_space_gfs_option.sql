-- Private spaces: an explicit owner option to use the connection server,
-- and a type on every invite link — ``spaces.private_gfs`` and
-- ``space_invite_tokens.via``: what the link may use, decided by the issuer
-- at mint time. ``'gfs'`` — a household that never met the issuer redeems it
-- through the connection-server relay (§D2b); allowed on a private space only
-- while ``private_gfs`` is ON. ``'internal'`` — the code carries no key-wrap
-- key, and the issuer refuses a redeem of it that arrives over the relay: it
-- works only for households paired with the issuer or reachable over the
-- mesh, and never touches a connection server. ``'gfs_legacy'`` — never
-- minted: a live link of a private space that this migration leaves OFF.
-- Every link minted before 0079 was relay-redeemable, so such a link stays
-- redeemable over the relay (grandfathered) until it is used up or expires,
-- and the FIRST household that joins through one turns the space's option ON
-- on the host (the normal ON path). Turning the option OFF deletes it like a
-- ``'gfs'`` link.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every path that touches this data. Space features live one
--       column each on ``spaces`` (``space_repo._save_statement``,
--       ``SpaceFeatures.to_columns``); no existing column says whether a
--       private space may use a GFS — ``gfs_channel_id`` (0077) is the
--       derived state (a channel exists right now), which the option
--       governs, and is NULL for an eligible space that has no remote member
--       yet. Invite links: the redeem paths (§D2 direct / routed, §D2b
--       bootstrap, a relayed §D2 from a ``space_session`` peer) all consume
--       the row through ``consume_invite_token``; ``gfs_id`` (0053) records
--       where a PUBLIC link's blob is parked and is NULL for every private
--       link (a private space is never listed, so its links are never
--       parked) — it cannot tell a relay-redeemable private link from an
--       internal one.
--   (2) Alternatives considered and rejected. Option: deriving it from
--       "has a link-joined member" (#815's rule) — that is exactly the
--       implicit behaviour the owner asked to make explicit, and it cannot
--       express "ON, no link member yet" or "OFF". Link type: leaving the
--       key-wrap key out of an internal code is not enough on its own — the
--       issuer's key-wrap key is the same on every code it ever minted, so a
--       holder of an internal token who saw any other link of this household
--       could graft it on and redeem through the relay; the issuer must know
--       the token's type. Encoding it in ``gfs_id`` (a sentinel value) would
--       overload a column whose readers revoke at a server by it. A new
--       table — one value per token, exactly the row's grain.
--   (3) Smallest possible change: two additive ``ADD COLUMN``s with CHECKs
--       and two backfills. ``private_gfs`` is ``NOT NULL DEFAULT 0`` (a new
--       private space is OFF); the first backfill turns it ON only for the
--       existing PRIVATE spaces that already have a link-joined household —
--       a ``space_session`` household in ``space_instances`` (a link-joined
--       member on the host; the link-joined host on a member), or a stored
--       channel (``gfs_channel_id``, 0077: a v_51 owner created one only for
--       a space with a link-joined member, and a member holds one only from
--       such an owner's grant — the member-side mirror of the same fact, so
--       a paired member's stub agrees with its host). Owner decision
--       2026-10-04: a space with only live invite links starts OFF. ``via``
--       is ``NOT NULL DEFAULT 'gfs'``; the second backfill marks the live
--       links of a private space left OFF ``'gfs_legacy'`` (grandfathered,
--       above) rather than adding a separate flag column — one value per
--       row, the column that already holds the link's type. Public / global
--       spaces' links keep ``'gfs'``: nothing changes for them.
ALTER TABLE spaces ADD COLUMN private_gfs INTEGER NOT NULL DEFAULT 0
    CHECK (private_gfs IN (0, 1));
ALTER TABLE space_invite_tokens ADD COLUMN via TEXT NOT NULL DEFAULT 'gfs'
    CHECK (via IN ('gfs', 'internal', 'gfs_legacy'));
UPDATE spaces
   SET private_gfs = 1
 WHERE space_type = 'private'
   AND (
        gfs_channel_id IS NOT NULL
        OR EXISTS (
            SELECT 1
              FROM space_instances si
              JOIN remote_instances ri ON ri.id = si.instance_id
             WHERE si.space_id = spaces.id
               AND ri.source = 'space_session'
        )
   );
UPDATE space_invite_tokens
   SET via = 'gfs_legacy'
 WHERE space_id IN (
        SELECT id FROM spaces WHERE space_type = 'private' AND private_gfs = 0
       );

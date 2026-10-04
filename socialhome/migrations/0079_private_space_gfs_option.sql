-- Private spaces: an explicit owner option to use the connection server,
-- and a type on every invite link — ``spaces.private_gfs`` and
-- ``space_invite_tokens.via``.
--
-- ``spaces.private_gfs``: the space OWNER's choice for a PRIVATE space —
-- may it use a connection server (GFS) at all. OFF: no invite link that
-- redeems through the relay, no opaque channel (v_51), no subscriptions.
-- ON: link-type invites are allowed, the owner registers the channel and
-- every member household connected to the channel's server takes a seat
-- (paired members included), so members reach each other while the host is
-- offline. Owner-only, federated in ``SPACE_CONFIG_CHANGED`` like every
-- feature flag, pinned on host inbound and taken only from the owner
-- household elsewhere (exactly like ``gfs_publish_mode``, 0076).
--
-- ``space_invite_tokens.via``: what the link may use, decided by the issuer
-- at mint time. ``'gfs'`` — today's link: its code carries the issuer's
-- key-wrap key, so a household that never met the issuer redeems it through
-- the connection-server relay (§D2b). ``'internal'`` — the code carries no
-- key-wrap key, and the issuer refuses a redeem of it that arrives over the
-- relay: it works only for households paired with the issuer or reachable
-- over the mesh, and never touches a connection server.
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
--   (3) Smallest possible change: two additive ``ADD COLUMN``s with CHECKs.
--       ``private_gfs`` is ``NOT NULL DEFAULT 0`` (a new private space is
--       OFF) and ONE backfill turns it ON for the existing PRIVATE spaces
--       that already use the relay — a ``space_session`` household in
--       ``space_instances`` (a link-joined member on the host; the
--       link-joined host on a member) or a live invite link (every link
--       minted before this migration is relay-redeemable) — so nothing that
--       works today breaks. ``via`` is ``NOT NULL DEFAULT 'gfs'``: every
--       existing link keeps exactly today's behaviour.
ALTER TABLE spaces ADD COLUMN private_gfs INTEGER NOT NULL DEFAULT 0
    CHECK (private_gfs IN (0, 1));
ALTER TABLE space_invite_tokens ADD COLUMN via TEXT NOT NULL DEFAULT 'gfs'
    CHECK (via IN ('gfs', 'internal'));
UPDATE spaces
   SET private_gfs = 1
 WHERE space_type = 'private'
   AND (
        EXISTS (
            SELECT 1
              FROM space_instances si
              JOIN remote_instances ri ON ri.id = si.instance_id
             WHERE si.space_id = spaces.id
               AND ri.source = 'space_session'
        )
        OR EXISTS (
            SELECT 1
              FROM space_invite_tokens t
             WHERE t.space_id = spaces.id
               AND t.uses_remaining > 0
               AND (
                    t.expires_at IS NULL
                    OR datetime(t.expires_at) > datetime('now')
               )
        )
   );

-- 0066 — the space authority key can rotate (federation v_44).
--
-- ``spaces.identity_public_key`` is the Ed25519 key every space-authority
-- signature verifies against (config, roster gossip, rekeys, GFS relays).
-- With ``delegated_admin_authority`` on, the owner shares its seed with
-- admin households, and until now nothing ever took it back: a household
-- demoted from admin stayed a co-authority forever. v_44 rotates the key
-- when a household loses its last admin seat (and when delegation is
-- turned off). Every rotation is an owner-signed certificate
-- (``socialhome/authority_cert.py``) carrying a monotonic ``key_epoch``.
--
-- Columns (all additive, all defaulting to 0 = "no rotation yet"):
--
-- * ``spaces.authority_key_epoch`` — the epoch of the key we pin. A receiver
--   applies a cert only when its epoch is HIGHER, so a replayed older cert
--   can never restore a key a revoked household holds. Written only by the
--   owner's ``rotate_authority_key`` and a receiver's ``adopt_authority_key``
--   (both compare-and-set), never by ``save``.
-- * ``spaces.authority_baseline_epoch`` — the highest epoch whose rotation
--   bundle this household already reset to. The baseline reset (config past
--   last-writer-wins, roster past the version guard, content key past the
--   ``rotated_by`` tiebreak) runs at most once per epoch, under a
--   compare-and-set claim on this column.
-- * ``spaces.authority_config_epoch``, ``space_remote_members.authority_epoch``
--   and ``space_keys.authority_epoch`` — the pin epoch in force when that
--   config / seat / content key was last written. The baseline reset only
--   overrides state written UNDER AN OLDER KEY: what a revoked household may
--   have inflated with it. State applied after the receiver already adopted
--   the new key (an inline cert, then newer owner traffic, then a late
--   bundle) is new-key-authorized and must not be rolled back.
-- * ``spaces.authority_seed_shared_epoch`` — on the owner, the key epoch at
--   which the signing seed was last shared with an admin household (NULL =
--   never). A revocation with delegation already OFF still rotates when a
--   seed was shared at the current epoch.
-- * ``spaces.mirror_gfs_id`` + ``spaces.gfs_rotation_seq`` — on a household
--   that merely FOLLOWS a public / global space, the GFS connection that
--   seated the mirror and the last ``authority_rotation_seq`` (that GFS's own
--   +1-per-re-pin counter) it showed. A follower re-pins from that GFS only,
--   and only to a higher seq. No existing field records which connection
--   server a mirror came from (``public_space_cache`` keys on the owning
--   instance), and the seq cannot share ``authority_key_epoch``: that column
--   orders OWNER-certified epochs, and a connection-server-supplied number
--   must never be compared with them.
--
-- Changing existing rows (the one backfill): pre-v44 builds shared the seed
-- with admin households and never retired it when delegation was turned
-- off, and nothing recorded that a seed was out. For every space THIS
-- household hosts that holds its seed, mark ``authority_seed_shared_epoch =
-- 0`` (the current epoch of every existing row): the next admin revocation
-- then rotates even with delegation off, retiring a seed that may still be
-- held. Spaces with no seed held, and stubs of other households' spaces,
-- are untouched. The cost is at most one extra rotation per legacy hosted
-- space; leaving it NULL would leave a pre-v44 seed authorized forever.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every path that touches the pin. ``identity_public_key`` is
--       written by ``SqliteSpaceRepo.save`` (INSERT only — excluded from the
--       upsert SET), ``set_space_pubkey`` (owner seed mint), and read by every
--       authority verifier (federation_inbound_service config, private_invite
--       _handler rekey / roster / snapshot, space_public_inbound,
--       space_subscriber_key_inbound). Seats are written by
--       ``SqliteSpaceRemoteMemberRepo`` (add / remove / apply_member_event /
--       reset_member_state) and content keys by ``SqliteSpaceKeyRepo.save``;
--       each stamps the per-row epoch from the space row in the same
--       statement. The whole-table dumps (backup, recovery kit, data export)
--       copy these tables with ``SELECT *`` and round-trip the new columns.
--   (2) Non-migration alternatives, rejected: deriving order from
--       ``config_sequence`` / ``roster_sequence`` / the content-key epoch
--       (all inflatable by any seed holder — exactly the party being
--       revoked); a chain-only ``prev_pk`` link (a receiver that missed one
--       rotation could never heal); freshness alone (a replay inside the
--       window re-pins the old key); timestamps instead of the per-row epoch
--       (second-resolution ``joined_at`` cannot order writes made in the same
--       second as the re-pin, and a revoked household can push continuously);
--       storing the whole cert (the owner re-signs on demand); a separate
--       "seeds shared" table (one nullable integer answers the question).
--   (3) Smallest change: eight additive columns, NOT NULL DEFAULT 0 (or NULL),
--       and one narrow backfill (above) touching only hosted spaces that hold
--       their seed; no rebuild, no index (always read with the row by primary
--       key).
ALTER TABLE spaces ADD COLUMN authority_key_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE spaces ADD COLUMN authority_baseline_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE spaces ADD COLUMN authority_config_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE spaces ADD COLUMN authority_seed_shared_epoch INTEGER;
ALTER TABLE space_remote_members ADD COLUMN authority_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE space_keys ADD COLUMN authority_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE spaces ADD COLUMN mirror_gfs_id TEXT;
ALTER TABLE spaces ADD COLUMN gfs_rotation_seq INTEGER NOT NULL DEFAULT 0;
UPDATE spaces SET authority_seed_shared_epoch = 0
 WHERE identity_private_key IS NOT NULL
   AND owner_instance_id = (
       SELECT instance_id FROM instance_identity WHERE id = 'self'
   );

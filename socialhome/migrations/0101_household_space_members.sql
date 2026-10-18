-- 0101 — everyone in the home is a member of its household spaces.
-- Follows 0100 (calendar bounds tz check).
--
-- A ``space_type = 'household'`` space means "everyone in your home is a
-- member automatically" (the create / settings copy). Until this release no
-- code seated anyone: a household space held its creator plus whoever joined
-- by hand, and kept whatever ``join_mode`` it was created with. From this
-- release the space service seats every active local user when a household
-- space is created, when a space moves into the household tier, and when a
-- new local user is provisioned, and stores ``join_mode = 'open'`` for every
-- household space. This repairs the rows written before that, once:
--
--   * ``join_mode`` → ``'open'`` on every household space THIS household
--     hosts (a household-tier stub another household hosts mirrors its
--     host's config and is left alone);
--   * a ``member`` seat for every active local user (``state = 'active'``,
--     not soft-deleted — ``user_repo.list_active``) with no seat yet, unless
--     banned from the space or a protected account below its ``min_age``
--     (the §CP.F1 gate ``ChildProtectionService.is_age_allowed`` applies).
--     An existing seat (any role) is never touched (INSERT OR IGNORE on the
--     (space_id, user_id) primary key).
--
-- Nothing is published: no "X joined" notification storm, no roster gossip.
-- A member household's roster converges through the host's roster snapshot /
-- §25.6 ``members`` catch-up like any other seat it missed.
--
-- A user who left a household space before this release is seated again:
-- until now there was no "everyone is a member" to leave, so every missing
-- seat reads the same. From here on a leave sticks — seating only happens on
-- the three events above, never on a reconcile.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path touching the data: ``SpaceService.create_space``
--       / ``update_config`` / ``request_join`` / ``add_member`` / the leave
--       and ban paths, ``space_repo.save_member`` / ``list_by_type`` /
--       ``is_banned``, ``UserService.provision`` / ``deprovision`` (soft-
--       delete keeps seats), the federation inbound config mirror (stubs
--       hosted elsewhere carry their host's ``space_type`` / ``join_mode``),
--       and the read gate (``read_space`` / ``_has_landing``).
--   (2) Non-migration alternatives considered and rejected:
--       * A reconcile on every boot (like the household chat's seats) — it
--         would undo every leave; seats here are real memberships, not
--         per-user chat state.
--       * A one-shot startup step behind an ``instance_config`` marker — the
--         same once-only effect as a migration, with a second bookkeeping
--         mechanism and no transaction around it.
--       * Leaving existing spaces as they are — the SPA tells users they
--         are members, and Browse would keep listing request-mode household
--         spaces nobody can be "automatically" in.
--   (3) Smallest change: one UPDATE and one INSERT OR IGNORE, both scoped to
--       household spaces this household hosts, plus two triggers. No new
--       column or table.
--
-- The guard (the invariant, in the database): a household space THIS
-- household hosts stores ``join_mode = 'open'``. ``BEFORE INSERT`` /
-- ``BEFORE UPDATE`` triggers refuse anything else, like 0098 / 0100 guard
-- their columns. Scoped to rows whose ``owner_instance_id`` is ours, so a
-- household-tier stub another household hosts — which mirrors whatever its
-- host (maybe an older one) sends — is never refused. A CHECK constraint
-- was rejected: it would need a full ``spaces`` rebuild and could not tell
-- our rows from mirrored ones. The writers of our own rows are
-- ``SpaceService.create_space`` / ``update_config`` (both store ``open``
-- for the household tier); inbound config only touches rows owned by the
-- sending household; a backup restore requires the same schema version,
-- so it only ever carries rows that already satisfy the guard.
--
-- Seats are NOT guarded: a member may leave a household space and stay
-- out, so "every local user holds a seat" is a policy the service applies
-- on create / tier change / new user, not an invariant.

UPDATE spaces
   SET join_mode = 'open'
 WHERE space_type = 'household'
   AND dissolved = 0
   AND join_mode <> 'open'
   AND owner_instance_id = (SELECT instance_id FROM instance_identity);

INSERT OR IGNORE INTO space_members(space_id, user_id, role, joined_at)
SELECT s.id,
       u.user_id,
       'member',
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
  FROM spaces s
  JOIN users u
    ON u.state = 'active'
   AND u.deleted_at IS NULL
 WHERE s.space_type = 'household'
   AND s.dissolved = 0
   AND s.owner_instance_id = (SELECT instance_id FROM instance_identity)
   AND NOT EXISTS (
       SELECT 1 FROM space_bans b
        WHERE b.space_id = s.id AND b.user_id = u.user_id
   )
   AND NOT (
       s.min_age > 0
       AND u.child_protection_enabled = 1
       AND COALESCE(u.declared_age, 0) < s.min_age
   );

CREATE TRIGGER IF NOT EXISTS trg_spaces_household_open_insert
BEFORE INSERT ON spaces
WHEN NEW.space_type = 'household'
 AND NEW.join_mode <> 'open'
 AND NEW.owner_instance_id = (SELECT instance_id FROM instance_identity)
BEGIN
    SELECT RAISE(ABORT, 'a household space hosted here must have join_mode open');
END;

CREATE TRIGGER IF NOT EXISTS trg_spaces_household_open_update
BEFORE UPDATE OF space_type, join_mode, owner_instance_id ON spaces
WHEN NEW.space_type = 'household'
 AND NEW.join_mode <> 'open'
 AND NEW.owner_instance_id = (SELECT instance_id FROM instance_identity)
BEGIN
    SELECT RAISE(ABORT, 'a household space hosted here must have join_mode open');
END;

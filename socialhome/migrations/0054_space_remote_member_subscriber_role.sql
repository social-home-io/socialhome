-- 0054 — a remote household may hold a read-only ``subscriber`` seat.
--
-- Widens ``space_remote_members.role`` from CHECK(role IN ('member','admin'))
-- to CHECK(role IN ('member','admin','subscriber')) so a household that
-- redeemed a Follower invite link can be seated as what it actually is: a
-- reader. Migration 0009 omitted the value on the stated premise that
-- "subscriber-role members never have a ``space_remote_members`` row
-- (subscriptions are tracked in a different table)" — true while a
-- subscriber was a purely LOCAL, GFS-relay-fed concept, and false the moment
-- #685 let an invite link carry a role and hand it to a stranger.
--
-- Why a rebuild (not an ALTER): SQLite cannot alter a CHECK constraint in
-- place; the documented "create new table → copy → drop → rename" dance is
-- the only route (https://sqlite.org/lang_altertable.html, §"Making Other
-- Kinds Of Table Schema Changes"). Same procedure migration 0042 used. The
-- table is rebuilt EXACTLY as it exists today (0001 shape + the 0009 ``role``
-- column + the 0031 ``member_version`` / ``tombstoned`` columns), changing
-- ONLY the CHECK; the FK, the PK and the one index are reproduced verbatim.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data. Everything goes
--       through ``repositories/space_remote_member_repo.py`` (add / remove /
--       get / get_including_tombstones / set_role / apply_member_event /
--       list_for_space / list_for_user / list_admin_instances). Its callers:
--       ``federation/private_invite_handler.py``,
--       ``federation/invite_token_redeem.py``,
--       ``services/federation_inbound_service.py``,
--       ``services/space_service.py``, ``services/space_approval_service.py``,
--       ``routes/spaces.py``. Every role-reading predicate is role-EXACT and
--       positive (``role == 'admin'`` in ``list_admin_instances`` and
--       ``space_approval_service._admin_keys``; ``actor.role != ADMIN`` →
--       drop, in ``apply_remote_admin_kick`` / ``apply_remote_admin_action``),
--       so a third value grants nothing anywhere: a subscriber row is not an
--       admin, is not an approval voter, and cannot drive a remote admin
--       action. The one "not-member means promote" shape,
--       ``invite_token_redeem`` line "if seat != MEMBER: set_role(seat)", is
--       exactly the write this migration exists to permit.
--
--       The audit also turned up a LIVE constraint violation the widening
--       repairs: ``space_service.remove_member`` and ``ban_member`` already
--       ship ``role=target.role`` on the v_23 LEFT roster gossip, and
--       ``target.role`` is read off ``space_members``, whose own CHECK admits
--       ``subscriber``. So kicking or banning a purely LOCAL subscriber today
--       sends ``role='subscriber'`` to every member household, where
--       ``apply_member_event`` binds it straight into this column and raises
--       IntegrityError — losing the whole roster event, tombstone included,
--       with the version guard making sure no later event heals it. The
--       on-disk vocabulary was narrower than the vocabulary the code already
--       emits; this brings the two back into line.
--
--   (2) Non-migration alternative considered and rejected. Model B: seat a
--       follower household on ``space_instances`` ALONE and let "no
--       ``space_remote_members`` row" mean "not a member, therefore no write
--       authority". It reuses existing shapes and needs no migration, and it
--       was rejected on four invariants that assume the row exists:
--         * Revocation is impossible. ``remove_remote_member`` is keyed on
--           (space_id, instance_id, user_id) FROM this table, and the SPA's
--           member list is built from it — a follower household with no row
--           is not listable and not kickable.
--         * The §D2b seat becomes immortal. ``instance_in_any_space`` reads
--           ``space_instances`` only, and the sole path that drops that row
--           is the "last remote member gone" prune inside
--           ``remove_remote_member``. With no member row that prune never
--           runs, so ``revoke_space_session_if_orphaned`` can never fire and
--           a stranger household keeps live directional session keys forever.
--         * ``_on_space_member_profile_updated`` resolves a member's home
--           instance from this mirror; with no row a follower's own profile
--           update is dropped as a suspected spoof.
--         * Peer households never learn the follower exists (the roster
--           gossip and the ``space_meta`` snapshot are both built from this
--           table), so the roster diverges by design.
--       Storing the seat in a federation event instead was rejected for the
--       reason 0009 gave for ``role`` itself: it must be queryable at REST
--       time without a round-trip. A parallel "space_remote_subscribers"
--       table was rejected under the CLAUDE.md "don't add a new table when an
--       existing one already proves what you need" rule — it would duplicate
--       the PK, the tombstone, the version counter and every reader.
--
--   (3) Smallest possible change. One CHECK gains one value. No column is
--       added, renamed or dropped; no row is changed (an explicit-column
--       copy preserves every value, including ``joined_at`` and the CRDT
--       ``member_version`` / ``tombstoned`` pair); no default changes; no
--       backfill. The rebuild is destructive in SHAPE only because SQLite
--       offers no in-place CHECK edit — it is value-preserving in content.
--
-- Nothing references ``space_remote_members``, so this is a pure child-table
-- rebuild; ``foreign_keys`` is still toggled off for the copy per the SQLite
-- procedure (the table is a child of ``spaces``). ``foreign_key_check`` below
-- is informational under ``executescript`` — a developer tripwire when run by
-- hand, exactly as in 0042.
PRAGMA foreign_keys=OFF;

CREATE TABLE space_remote_members_new (
    space_id       TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    instance_id    TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    user_pk        TEXT,
    display_name   TEXT,
    joined_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- The one change: 'subscriber' joins the vocabulary. 'owner' stays out —
    -- ownership is a local-only privilege (dissolve, transfer) that cannot
    -- cross households, exactly as 0009 reasoned.
    role           TEXT NOT NULL DEFAULT 'member'
                   CHECK(role IN ('member', 'admin', 'subscriber')),
    member_version INTEGER NOT NULL DEFAULT 0,
    tombstoned     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (space_id, instance_id, user_id)
);
INSERT INTO space_remote_members_new
    (space_id, instance_id, user_id, user_pk, display_name, joined_at,
     role, member_version, tombstoned)
SELECT
    space_id, instance_id, user_id, user_pk, display_name, joined_at,
    role, member_version, tombstoned
FROM space_remote_members;
DROP TABLE space_remote_members;
ALTER TABLE space_remote_members_new RENAME TO space_remote_members;
-- DROP TABLE took the index with it; recreate it verbatim (0001).
CREATE INDEX IF NOT EXISTS idx_space_remote_members_instance_user
    ON space_remote_members(instance_id, user_id);

PRAGMA foreign_key_check;
PRAGMA foreign_keys=ON;

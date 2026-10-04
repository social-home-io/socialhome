-- 0080 — an invite link may grant a ``moderator`` seat.
--
-- Widens the ``space_invite_tokens.role`` CHECK (migration 0053) by one
-- value:
--
--   member, subscriber, admin → member, subscriber, admin, moderator
--
-- The owner asked for "Moderator" in the invite-link dialog. Who may mint
-- one mirrors who may promote to moderator (``role_change_allowed``: the
-- owner or an admin). The seat is the ISSUER's decision, stored on the row
-- and read back by every redeem path — exactly like the other three seats.
--
-- Why a rebuild (not an ALTER): SQLite cannot alter a CHECK constraint in
-- place; the documented "create new table → copy → drop → rename" dance is
-- the only route (https://sqlite.org/lang_altertable.html, §"Making Other
-- Kinds Of Table Schema Changes"). Same procedure as 0042, 0054 and 0065.
-- The table is rebuilt EXACTLY as it exists after 0079 — the 0001 columns,
-- the 0053 ``role`` / ``gfs_*`` / ``uses_total`` columns and the 0079 ``via``
-- column with its CHECK, in that order — changing ONLY the role CHECK; the
-- FK (``spaces`` ON DELETE CASCADE), the PK and the one index are reproduced
-- verbatim. The table has no trigger and no table holds a foreign key INTO
-- it, so dropping it cascades nowhere.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data. WRITERS:
--       ``SpaceService.create_invite_link`` (the SPA mint — the only path
--       that takes a role), ``invite_remote_user`` and the §D2 join-request
--       approval (both go straight to ``SqliteSpaceRepo.create_invite_token``
--       with the implicit ``member``). READERS: the atomic
--       ``consume_invite_token`` (local ``accept_invite_token``, the §D2 /
--       §D2b ``_consume_seat_and_build_ack``), ``get_live_invite_token``,
--       ``list_live_invite_tokens`` and the re-ACK lookup. Every reader
--       hands the stored role to a seat whose own CHECK already admits
--       ``moderator`` (``space_members`` / ``space_remote_members``, 0065),
--       so the token table is the one place the vocabulary is narrower.
--       The repo has no role allow-list of its own; the CHECK is it.
--
--   (2) Non-migration alternatives considered and rejected:
--       * Mint a ``member`` link and promote on redeem, deciding "this one
--         was meant as moderator" from somewhere else — there is nowhere
--         else: the seat is the issuer's decision and must live on the
--         issuer's row (0053's whole point), never in the token string or
--         the redeem request, where it would be attacker-controlled.
--       * Reuse ``admin`` plus a pending elevation the owner then approves
--         as moderator — overloads the admin link's meaning, makes an admin
--         (who may promote to moderator) unable to mint it, and a moderator
--         seat needs no seed or settings authority to guard.
--       * A side table ``(token, granted_role)`` or a flag column — two
--         sources of truth for one seat, which every reader would have to
--         combine; the role column already holds exactly this.
--
--   (3) Smallest possible change. One CHECK gains one value. No column is
--       added, renamed or dropped; no row is changed (an explicit-column
--       copy preserves every value); no default changes; no backfill. The
--       rebuild is destructive in SHAPE only because SQLite offers no
--       in-place CHECK edit — it is value-preserving in content.
--
-- ``foreign_keys`` is toggled off for the copy per the SQLite procedure (the
-- table is a child of ``spaces``). ``foreign_key_check`` is informational
-- under ``executescript`` — a developer tripwire when run by hand, exactly as
-- in 0042 / 0054 / 0065.
PRAGMA foreign_keys=OFF;

CREATE TABLE space_invite_tokens_new (
    token          TEXT PRIMARY KEY,
    space_id       TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    created_by     TEXT NOT NULL,
    uses_remaining INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at     TEXT,
    -- The one change: 'moderator' joins the vocabulary. 'owner' stays out —
    -- ownership moves only through transfer_ownership.
    role           TEXT NOT NULL DEFAULT 'member'
                   CHECK(role IN ('member', 'subscriber', 'admin', 'moderator')),
    gfs_id         TEXT,
    gfs_token      TEXT,
    gfs_url        TEXT,
    uses_total     INTEGER,
    via            TEXT NOT NULL DEFAULT 'gfs'
                   CHECK (via IN ('gfs', 'internal', 'gfs_legacy'))
);
INSERT INTO space_invite_tokens_new
    (token, space_id, created_by, uses_remaining, created_at, expires_at,
     role, gfs_id, gfs_token, gfs_url, uses_total, via)
SELECT
    token, space_id, created_by, uses_remaining, created_at, expires_at,
    role, gfs_id, gfs_token, gfs_url, uses_total, via
FROM space_invite_tokens;
DROP TABLE space_invite_tokens;
ALTER TABLE space_invite_tokens_new RENAME TO space_invite_tokens;
-- DROP TABLE took the index with it; recreate it verbatim (0001).
CREATE INDEX IF NOT EXISTS idx_space_invite_tokens_space
    ON space_invite_tokens(space_id);

PRAGMA foreign_key_check;
PRAGMA foreign_keys=ON;

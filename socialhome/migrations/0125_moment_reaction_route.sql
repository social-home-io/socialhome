-- 0125 — reactions reach authors further away (v_67; owner decision 9,
-- sign-off 2, 2026-10-25).
--
-- A moment that reached this household through one or two relays keeps how
-- a reaction gets back to its origin: the origin's key-wrap public key, the
-- neighbour it came from (``via``) and the relays' return tokens. A
-- reaction can come days later (a moment lives up to 30 days) and after a
-- restart, so this has to be on disk; it is per moment because the tokens
-- and the neighbour are.
--
--   reaction_route  JSON ``{"kw": "<64 hex>", "via": "<instance id>",
--                   "rt": ["<token>", …], "rt_suite": "aesgcm-256"}`` —
--                   NULL for a moment without a complete route (written
--                   here, delivered directly by a paired household, through
--                   a GFS, or along a path with an older relay).
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that writes ``moments``: the household inbound
--       create (``FederationInboundService._on_moment_created`` stores the
--       first copy and re-saves on a lower hop — both now set the route),
--       ``SqliteMomentRepo.save`` (its ``ON CONFLICT`` update names its
--       columns, so a re-save, ``mark_public`` (#1169) and a save
--       (``moment_saves``, #1177) keep the route), the GFS inbound
--       (``received_via = 'gfs'``, stores only a moment it does not hold),
--       ``_tombstone_rows`` / ``tombstone`` (every delete — wiped there and
--       by the trigger below), ``prune_expired`` (the row goes, and the
--       route with it) and the relay (``relay_inbound`` reads the create
--       payload, not the row). Reactions themselves keep using
--       ``moment_reactions`` unchanged.
--   (2) Alternatives rejected:
--       * keep routes in memory — a reaction can come days later and the
--         household restarts in between;
--       * ``remote_instances.remote_keywrap_pk`` — holds the key-wrap key
--         of paired households only, and the origin is by definition not
--         one;
--       * a new origin-keys table — the tokens and the neighbour are per
--         moment anyway, so a second table would only split one record.
--   (3) Smallest change: one NULL-defaulted column, no backfill, no index
--       (read by primary key only). The CHECK keeps it valid JSON and on
--       relayed rows only; the trigger keeps a tombstone from holding one.

ALTER TABLE moments ADD COLUMN reaction_route TEXT NULL
    CHECK (
        reaction_route IS NULL
        OR (json_valid(reaction_route) AND received_via = 'household')
    );

CREATE TRIGGER IF NOT EXISTS trg_moments_drop_reaction_route_on_tombstone
AFTER UPDATE OF deleted_at ON moments
WHEN NEW.deleted_at IS NOT NULL AND NEW.reaction_route IS NOT NULL
BEGIN
    UPDATE moments SET reaction_route = NULL WHERE id = NEW.id;
END;

-- Removal watermarks (#1178 review, F3). A routed reaction can arrive out
-- of order, and a relay can replay one: the origin applies adds and
-- removals in the order they were made (``occurred_at``, kept to the
-- millisecond). A removal deletes the ``moment_reactions`` row, so without
-- this table nothing on disk remembers it, and after a restart (the replay
-- cache is in memory) a replayed older add would bring the reaction back.
--
--   moment_reaction_removals  one row per (moment, reactor) whose latest
--                             routed change was a removal: when it was
--                             made (``YYYY-MM-DD HH:MM:SS.mmm`` UTC, the
--                             ``reacted_at`` shape with milliseconds).
--
-- Audit (CLAUDE.md "audit before a migration"):
--   (1) Every reader of ``moment_reactions`` — ``list_reactions``,
--       ``count_engagement_for`` (counts + ``my_reaction``), the
--       notification path (``MomentReactionChanged`` published only when
--       ``apply_reaction_at`` changed a row), ``_tombstone_rows`` — reads
--       that table only; a watermark in its own table never shows as a
--       reaction. Neither table is in the backup (moments are ephemeral)
--       nor in the data export.
--   (2) Alternatives rejected:
--       * keep the removed row with ``emoji`` NULL — ``emoji`` is NOT NULL
--         since 0001, so that is a table rebuild, and every reader above
--         would have to learn to skip it;
--       * keep removals in memory (as before) — lost on restart, the bug;
--       * a longer-lived replay cache on disk — would store every sealed
--         reaction's digest instead of one row per removed reaction.
--   (3) Smallest change: one small table, no backfill (0125 is unreleased).
--       Its rows go with their moment: ``ON DELETE CASCADE`` on prune, the
--       trigger below on a tombstone, and a reaction row replaces its
--       watermark (the two never coexist — a database invariant, not a
--       service convention).

CREATE TABLE IF NOT EXISTS moment_reaction_removals (
    moment_id        TEXT NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
    reactor_user_id  TEXT NOT NULL,
    removed_at       TEXT NOT NULL,
    PRIMARY KEY (moment_id, reactor_user_id)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS trg_moments_drop_reaction_removals_on_tombstone
AFTER UPDATE OF deleted_at ON moments
WHEN NEW.deleted_at IS NOT NULL
BEGIN
    DELETE FROM moment_reaction_removals WHERE moment_id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_moment_reactions_replace_removal_on_insert
AFTER INSERT ON moment_reactions
BEGIN
    DELETE FROM moment_reaction_removals
     WHERE moment_id = NEW.moment_id AND reactor_user_id = NEW.reactor_user_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_moment_reactions_replace_removal_on_update
AFTER UPDATE ON moment_reactions
BEGIN
    DELETE FROM moment_reaction_removals
     WHERE moment_id = NEW.moment_id AND reactor_user_id = NEW.reactor_user_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_moment_reaction_removals_drop_reaction
AFTER INSERT ON moment_reaction_removals
BEGIN
    DELETE FROM moment_reactions
     WHERE moment_id = NEW.moment_id AND reactor_user_id = NEW.reactor_user_id;
END;

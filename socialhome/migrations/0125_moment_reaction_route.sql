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

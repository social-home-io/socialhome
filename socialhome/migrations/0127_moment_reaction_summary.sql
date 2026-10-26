-- 0127 — everyone sees the full reaction count (v_69; owner decision 10,
-- 2026-10-25). Follows 0126.
--
-- Only a moment's author household holds every reaction to it (paired,
-- 2–3-hop and GFS followers' all reach it since v_67 / v_68). It now sends
-- the totals — per-emoji counts and the total, never who — at fixed
-- checkpoints. A household that holds someone else's moment keeps the
-- newest totals it got:
--
--   reaction_summary  JSON ``{"rev": <ms>, "counts": {"<emoji>": n, …},
--                     "total": n}`` — ``rev`` is the author household's
--                     send time (Unix ms, only ever grows), ``counts`` at
--                     most 8 emoji. NULL until totals arrive, and always on
--                     a moment written here (the author's household counts
--                     its own ``moment_reactions`` rows).
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the data. Writers of ``moments``:
--       ``SqliteMomentRepo.save`` (its ``ON CONFLICT`` update names its
--       columns, so a re-save from a closer relay, ``mark_public`` and the
--       GFS path keep the summary), the household inbound create and the
--       GFS inbound (store only the content row), ``_tombstone_rows`` /
--       ``tombstone`` (every delete — the trigger below wipes the summary,
--       as 0125 does the reaction route), ``prune_expired`` (the row goes,
--       with its summary), ``set_reaction_route`` (another column). Readers:
--       ``count_engagement_for`` / ``_moment_dict`` (the API counts) and the
--       new ``get_reaction_summaries``; ``moment_reactions`` itself — the
--       rows the author household counts and every other household's own
--       reactions — is untouched. Neither ``moments`` column is in the
--       backup or the data export (moments are ephemeral).
--   (2) Alternatives rejected:
--       * keep totals in memory — lost on every restart, and a moment lives
--         up to 30 days: the inbox would fall back to the local count until
--         the next checkpoint, which may never come;
--       * synthetic ``moment_reactions`` rows — that table's key is
--         (moment, reactor): a count is not a person, and every reader
--         (``list_reactions``, notifications, ``my_reaction``, the routed
--         ordering against ``moment_reaction_removals``) would have to learn
--         to skip them;
--       * compute at read time — only the author's household can, it holds
--         the reactions;
--       * a separate table — one small value per moment, read with the
--         moment, gone with the moment: a column on the row is the record.
--   (3) Smallest change: one NULL-defaulted column, no backfill, no index
--       (read by primary key only). The CHECK keeps it valid JSON and off
--       moments written here; the trigger keeps a tombstone from holding
--       one.

ALTER TABLE moments ADD COLUMN reaction_summary TEXT NULL
    CHECK (
        reaction_summary IS NULL
        OR (
            json_valid(reaction_summary)
            AND received_via IN ('household', 'gfs')
        )
    );

CREATE TRIGGER IF NOT EXISTS trg_moments_drop_reaction_summary_on_tombstone
AFTER UPDATE OF deleted_at ON moments
WHEN NEW.deleted_at IS NOT NULL AND NEW.reaction_summary IS NOT NULL
BEGIN
    UPDATE moments SET reaction_summary = NULL WHERE id = NEW.id;
END;

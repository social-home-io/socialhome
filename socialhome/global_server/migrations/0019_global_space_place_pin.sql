-- GFS 0019 — a global space's optional place pin.
-- Follows 0018 (drop client inbox url).
--
-- Owner decision (2026-10-19): the owner or an admin of a GLOBAL space may
-- pin it to a place (a town, a club house), so a household browsing the
-- directory can sort it by distance from its own home — computed in that
-- household's browser; the viewer's location never reaches a GFS. The pin is
-- the space's own published listing metadata, like its name and
-- description, and arrives in the owner's signed publish body (``place``).
-- It is never a person's or a household's location: the owner chooses it,
-- and the household side never defaults it to its home.
--
-- CLAUDE.md "audit before a migration":
-- 1. Audited every path that touches a listing: ``publish_space`` (the
--    owner's signed body — the only writer), ``upsert_space`` /
--    ``_row_to_space`` (the only SQL), cluster ``NODE_SYNC_SPACE`` gossip
--    (``_space_to_wire`` / ``_wire_to_space``), ``public_space_dict`` (the
--    directory and detail reads). Nothing existing carries a point.
-- 2. Non-migration alternatives rejected: keeping it in memory (lost on
--    restart, and a cluster peer could not serve it); packing it into
--    ``description`` / ``about_markdown`` (user-visible text, no CHECK);
--    asking the owning household per directory read (the GFS answers the
--    whole directory with no household online).
-- 3. Smallest change: two additive NULL-defaulted columns, no backfill (no
--    listing has a pin until its owner sets one), no index (read with the
--    row). A CHECK keeps a row to a whole, in-range point or none.

ALTER TABLE global_spaces ADD COLUMN place_lat REAL;
ALTER TABLE global_spaces ADD COLUMN place_lon REAL
    CHECK (
        (place_lat IS NULL AND place_lon IS NULL)
        -- Spelled out: a NULL in a comparison is not FALSE, so without the
        -- IS NOT NULLs half a point would pass the CHECK.
        OR (
            place_lat IS NOT NULL AND place_lon IS NOT NULL
            AND place_lat BETWEEN -90 AND 90
            AND place_lon BETWEEN -180 AND 180
        )
    );

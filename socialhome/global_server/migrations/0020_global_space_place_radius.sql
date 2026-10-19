-- GFS 0020 — a global space's optional radius hint beside its place pin.
-- Follows 0019 (global space place pin).
--
-- Owner request (2026-10-19): next to a global space's place pin (0019) its
-- owner or an admin may give a radius HINT — "best for people within 10 km"
-- (0.5–50 km). A hint only: no GFS and no household ever checks it at join
-- or follow. It is part of the owner's signed ``place`` object
-- (``{lat, lon, radius_km}``) under the existing ``place_pin`` capability —
-- 0019 has not shipped in any release, so no GFS verifies ``place`` without
-- it — and a reader's Discover compares it with the distance from its OWN
-- home, in its browser; the viewer's location never reaches a GFS.
--
-- CLAUDE.md "audit before a migration":
-- 1. Audited every path that touches a listing's pin: ``publish_space``
--    (the owner's signed body — the only writer), ``upsert_space`` /
--    ``_row_to_space`` (the only SQL), cluster ``NODE_SYNC_SPACE`` gossip
--    (``_space_to_wire`` / ``_wire_to_space``), ``public_space_dict`` (the
--    directory and detail reads).
-- 2. Non-migration alternatives rejected: rewriting 0019 in place (a
--    development GFS that ran it would lack the column and fail every
--    upsert); keeping it in memory (lost on restart, a cluster peer could not
--    serve it); packing it into ``description`` (user-visible text, no
--    CHECK); asking the owning household per read (the GFS answers the
--    directory with no household online).
-- 3. Smallest change: one additive NULL-defaulted column, no backfill, no
--    index. A CHECK keeps it in bounds and only beside a whole pin.

ALTER TABLE global_spaces ADD COLUMN place_radius_km REAL
    CHECK (
        place_radius_km IS NULL
        OR (
            place_lat IS NOT NULL AND place_lon IS NOT NULL
            AND place_radius_km BETWEEN 0.5 AND 50
        )
    );

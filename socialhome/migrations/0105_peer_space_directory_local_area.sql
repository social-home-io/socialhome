-- 0105 — the paired-household space directory keeps a local community's area.
-- Follows 0104 (space event mirror tz).
--
-- A public space can be a local community: bound to a centre + radius, and
-- a household joins only while the home it shares with the host lies inside
-- (owner decision 2026-10-19). Paired households see the area BEFORE they
-- ask to join — in Discover and on the space's About page, with an
-- "In your area" / "Outside your area" badge computed against their own
-- home. The host sends it in its ``SPACE_DIRECTORY_SYNC`` snapshot (v_59,
-- ``FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA``); this is where the
-- receiving household keeps it.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the directory and the area:
--       ``PeerDirectoryService._build_snapshot`` (sender — full snapshot,
--       replaced atomically per peer), ``PeerDirectoryHandler._on_snapshot``
--       (receiver), ``SqlitePeerSpaceDirectoryRepo.replace_snapshot`` /
--       ``list_all`` / ``list_for_instance`` / ``clear_instance`` (the only
--       SQL on the table), ``GET /api/peer_spaces`` (the only reader),
--       unpair cleanup (``clear_instance``), and the host side
--       (``spaces.lat`` / ``lon`` / ``radius_km`` — already columns since
--       0001, written by create / ``set_local_area``, kept host-local by
--       ``keep_local_space_state``). ``public_space_cache`` (the GFS
--       directory) also has lat / lon / radius_km, but a GFS never carries
--       a local community (public is never on a GFS) and the two caches are
--       different trust domains — reusing it would mix them.
--   (2) Non-migration alternatives considered and rejected: computing it at
--       read time (the receiver has no other source — the area lives only
--       on the host); keeping the snapshot in memory (lost on restart, and
--       a snapshot is only re-sent on a change or a new pairing, so the
--       area would vanish until the host next edits a public space);
--       packing it into an existing TEXT column (``description`` /
--       ``category`` are user-visible fields, and a JSON blob would carry
--       no range CHECK).
--   (3) Smallest change: three additive NULL-defaulted columns, no backfill
--       (existing rows have no area — the next snapshot from a v_59 host
--       fills them), no index (the table is read whole, a few dozen rows),
--       plus a CHECK on the last one so a row holds a whole, in-range area
--       or none — never half an area or a radius the sender could not set.

ALTER TABLE peer_space_directory ADD COLUMN lat REAL;
ALTER TABLE peer_space_directory ADD COLUMN lon REAL;
ALTER TABLE peer_space_directory ADD COLUMN radius_km REAL
    CHECK (
        (lat IS NULL AND lon IS NULL AND radius_km IS NULL)
        -- Spelled out: a NULL in a comparison is not FALSE, so without the
        -- IS NOT NULLs half an area would pass the CHECK.
        OR (
            lat IS NOT NULL AND lon IS NOT NULL AND radius_km IS NOT NULL
            AND lat BETWEEN -90 AND 90
            AND lon BETWEEN -180 AND 180
            AND radius_km BETWEEN 0.5 AND 50
        )
    );

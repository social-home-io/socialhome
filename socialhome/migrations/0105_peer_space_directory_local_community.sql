-- 0105 — the paired-household space directory knows which entries are local
-- communities, and the host's verdict for this household.
-- Follows 0104 (space event mirror tz).
--
-- A public space can be a local community: bound to a centre + radius, and
-- a household joins only while the home it shares with the host lies inside
-- (owner decision 2026-10-19). People's locations are not shared by design,
-- so the CENTRE never leaves the host: a paired household learns only
-- "a local community, within X km" and the host's own in-area verdict for
-- this household's shared home (``in_area``), in the host's
-- ``SPACE_DIRECTORY_SYNC`` snapshot (v_59,
-- ``FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA``). This is where the
-- receiving household keeps them, for Discover and the About page.
--
-- Rewritten in place before release (2026-10-19): the first revision of this
-- migration stored the centre (``lat`` / ``lon``). No release ever contained
-- it; a development database that ran it is reshaped by 0106.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the directory and the area:
--       ``PeerDirectoryService`` (sender — one snapshot per peer, replaced
--       atomically), ``PeerDirectoryHandler._on_snapshot`` (receiver),
--       ``SqlitePeerSpaceDirectoryRepo.replace_snapshot`` / ``list_all`` /
--       ``list_for_instance`` / ``clear_instance`` (the only SQL on the
--       table), ``GET /api/peer_spaces`` (the only reader), unpair cleanup
--       (``clear_instance``), and the host side (``spaces.lat`` / ``lon`` /
--       ``radius_km`` — columns since 0001, kept host-local by
--       ``keep_local_space_state``). ``public_space_cache`` (the GFS
--       directory) is a different trust domain and never lists a public
--       space.
--   (2) Non-migration alternatives considered and rejected: computing the
--       verdict at read time (the receiver cannot — it has no centre, by
--       design); keeping the snapshot in memory (lost on restart, and a
--       snapshot is only re-sent on a change, a new pairing or the peer's
--       next start); packing it into an existing TEXT column
--       (``description`` / ``category`` are user-visible, and a blob would
--       carry no CHECK).
--   (3) Smallest change: two additive NULL-defaulted columns, no backfill
--       (existing rows are not local communities until the next v_59
--       snapshot), no index (the table is read whole, a few dozen rows).
--       ``radius_km`` NOT NULL *is* the "local community" flag, so no third
--       column; ``in_area`` exists only on a local community and is a
--       boolean or unknown (NULL).

ALTER TABLE peer_space_directory ADD COLUMN radius_km REAL
    CHECK (radius_km IS NULL OR radius_km BETWEEN 0.5 AND 50);
ALTER TABLE peer_space_directory ADD COLUMN in_area INTEGER
    CHECK (in_area IS NULL OR (radius_km IS NOT NULL AND in_area IN (0, 1)));

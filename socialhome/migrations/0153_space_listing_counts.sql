-- A space's listing counts — members AND followers — in both discovery caches.
--
-- Owner decision 2026-11-23: the member and follower counts are part of the
-- space metadata a host shares — signed in its GFS listing publish, and in
-- the ``SPACE_DIRECTORY_SYNC`` snapshot to paired households. A host (or a
-- GFS) that declares no count must show NO number, never ``0``.
--
-- Audit per the CLAUDE.md "Before adding a SQL migration" rule:
--
-- 1. Existing code paths touching this data. ``public_space_cache`` has one
--    writer, ``PublicSpaceDiscoveryService`` → ``SqlitePublicSpaceRepo.upsert``
--    (the GFS directory poll), and its readers — ``routes/public_spaces.py``
--    (global Discover), ``GfsSpaceMirrorService`` (mirror provenance), the
--    maintenance purge and now the space page's counts for a household that
--    only follows a space. Its ``member_count`` was filled from the GFS's
--    ``subscriber_count`` (relay seats, almost always 0) — the bug.
--    ``peer_space_directory`` has one writer, ``PeerDirectoryHandler`` →
--    ``replace_snapshot``, and readers ``routes/peer_spaces.py`` (paired
--    Discover) and the space page; its ``member_count`` is already exact (a
--    paired host always sends one) — only a follower count is missing.
-- 2. Non-migration alternatives considered and rejected: (a) fetch the
--    counts from the GFS / the host per Discover row — one request per row
--    per page load, against a server that may be down; (b) keep
--    ``public_space_cache.member_count`` NOT NULL and read "no number" off a
--    NULL ``follower_count`` — couples two values a receiver validates one by
--    one, and a hidden rule in every reader; (c) a new nullable column beside
--    the old one — leaves a dead ``member_count`` column behind. The counts
--    belong with the cached listing they describe.
-- 3. Minimality. ``peer_space_directory``: one additive NULL column.
--    ``public_space_cache``: its ``member_count`` must become NULLable ("no
--    number"), which SQLite can only do by rebuilding the table. It is a
--    cache that refills on the next directory poll (minutes) — the same kind
--    of rebuild 0038 already did to it — and nothing references it. Every
--    row is copied; only ``member_count`` is reset to NULL, because each
--    stored value is the GFS relay-seat count, not the space's members.

ALTER TABLE peer_space_directory
    ADD COLUMN follower_count INTEGER
    CHECK (follower_count IS NULL OR follower_count >= 0);

CREATE TABLE public_space_cache_new (
    space_id        TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    emoji           TEXT,
    lat             REAL,
    lon             REAL,
    radius_km       REAL,
    -- The host's declared counts; NULL = it declared none (an older host or
    -- an older GFS) — shown as no number, never 0.
    member_count    INTEGER CHECK (member_count IS NULL OR member_count >= 0),
    follower_count  INTEGER CHECK (follower_count IS NULL OR follower_count >= 0),
    -- Mirror ``spaces.min_age`` CHECK so a malformed inbound discover entry
    -- cannot quietly land.
    min_age         INTEGER NOT NULL DEFAULT 0
                    CHECK(min_age IN (0, 13, 16, 18)),
    -- Discovery category (§23.50). No CHECK: this cache ingests remote values
    -- that may not match our taxonomy (normalized to "general" on read).
    category        TEXT,
    cached_at       TEXT NOT NULL DEFAULT (datetime('now')),
    join_mode       TEXT NOT NULL DEFAULT 'invite_only',
    allow_subscribers INTEGER NOT NULL DEFAULT 0
                    CHECK(allow_subscribers IN (0,1))
);

INSERT INTO public_space_cache_new(
    space_id, instance_id, name, description, emoji, lat, lon, radius_km,
    member_count, follower_count, min_age, category, cached_at, join_mode,
    allow_subscribers
)
SELECT space_id, instance_id, name, description, emoji, lat, lon, radius_km,
       NULL, NULL, min_age, category, cached_at, join_mode, allow_subscribers
  FROM public_space_cache;

DROP TABLE public_space_cache;

ALTER TABLE public_space_cache_new RENAME TO public_space_cache;

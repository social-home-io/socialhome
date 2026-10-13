-- 0097 — ``household_zones``: the household's own named places (Home,
-- School, Work…) as labelled circles. Follows 0096 (space zone icons).
--
-- Local to the household and never federated — unlike the per-space
-- ``space_zones`` catalogue. The server matches a member's GPS to these
-- zones on ``POST /api/presence/location`` when the client sends no
-- ``zone_name`` (standalone, external clients), so presence reads
-- "Home" / "School" / "Away" without Home Assistant. Zone names stay in
-- the household: space-bound presence strips ``zone_name`` (§23.8.6).
--
-- Columns: centre ``latitude`` / ``longitude`` (4 dp, validated by
-- ``domain.space.validate_zone_coord``), ``radius_m`` (25 m – 50 km),
-- ``color`` (``#RRGGBB`` / NULL), ``icon`` (one emoji / NULL),
-- ``is_home`` (at most one Home zone — the partial unique index), and
-- ``external_id``: NULL = created in Social Home (editable); set = mirrored
-- by a platform sync (Home Assistant's zone entity id, e.g. ``zone.home``)
-- and read-only through the API. ``created_at`` / ``updated_at`` are UTC
-- (tz-aware ISO 8601, written by the service).
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every code path that touches presence and zones:
--       ``routes/presence.py`` (``PresenceLocationView`` derives the state
--       from the client's ``zone_name``), ``services/presence_service.py``
--       (``update_location`` — accuracy gate, 4-dp truncation,
--       ``PresenceUpdated``), ``repositories/presence_repo.py`` (stores the
--       zone label only, never a zone definition), the HA home location
--       (``platform/ha_home_location.py`` → ``instance_identity``: one
--       point, no radius, HA-only), and ``space_zones`` (0010 / 0085 / 0086
--       / 0096 + ``space_zone_repo`` / service / sync). None of them holds a
--       household-level zone list.
--   (2) Non-migration alternatives considered and rejected:
--       * Reusing ``space_zones`` — per space, federated to every member
--         household, sync-stamped and tombstoned; household zone names must
--         never leave the household.
--       * A JSON blob in ``instance_config`` — loses the radius range, the
--         unique name / external id and the one-Home rule the DB enforces
--         here, and every edit rewrites the whole list.
--       * HA-only zones read live from Home Assistant — standalone mode has
--         no HA, and the GPS match must not depend on an HA round trip.
--   (3) Smallest change: one new table and its indexes; no existing row or
--       table changes.

CREATE TABLE IF NOT EXISTS household_zones (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL COLLATE NOCASE UNIQUE,
    latitude    REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude   REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    radius_m    INTEGER NOT NULL CHECK (radius_m BETWEEN 25 AND 50000),
    color       TEXT,
    icon        TEXT,
    is_home     INTEGER NOT NULL DEFAULT 0 CHECK (is_home IN (0, 1)),
    external_id TEXT UNIQUE,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- At most one Home zone.
CREATE UNIQUE INDEX IF NOT EXISTS idx_household_zones_one_home
    ON household_zones(is_home) WHERE is_home = 1;

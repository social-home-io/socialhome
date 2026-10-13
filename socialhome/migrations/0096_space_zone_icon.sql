-- 0096 — ``space_zones.icon``: an optional single emoji drawn on the space
-- map for a zone (like Home Assistant's zone icons). Follows 0095 (GFS
-- channel withdrawals).
--
-- NULL = no icon (the map draws the plain coloured circle). The value is
-- validated as exactly one emoji by ``domain.space.validate_zone_icon`` on
-- every write path: the local API, ``SPACE_ZONE_UPSERTED`` and the
-- ``space_zones`` sync resource.
--
-- The ``space_zones_sync_seq_update`` trigger (0086) compares every synced
-- column; it is recreated with ``icon`` added so an icon-only edit still
-- bumps the §25.6 change stamp and reaches households by incremental sync.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every code path touching ``space_zones``:
--       ``repositories/space_zone_repo.py`` (list / get / upsert / delete
--       tombstone), ``SpaceZoneService`` (create / update / delete +
--       ``SpaceZoneUpserted``), ``routes/space_zones.py``,
--       ``SpaceZoneOutboundService`` + the ``SPACE_ZONE_UPSERTED`` inbound
--       handler (``federation_inbound/space_content.py``), the sync
--       exporter (``federation/sync/space/exporters/zones.py``) + receiver,
--       ``RealtimeService`` (``space_zone_changed``), and the 0085 tombstone
--       + 0086 sync-stamp triggers. None of them carries an icon today.
--   (2) Non-migration alternatives considered and rejected:
--       * Overloading ``color`` / ``name`` (e.g. an emoji prefix in the name)
--         — the name is unique per space and shown as text everywhere;
--         ``color`` has a ``#RRGGBB`` contract every peer validates.
--       * Client-only / per-household storage — the icon is space content
--         every member should see; it must replicate with the zone row.
--       * Computing it at read time — there is nothing to derive it from;
--         it's an admin choice.
--   (3) Smallest change: one ``ADD COLUMN`` with a NULL default, no backfill,
--       no rewrite of any existing row; the trigger is dropped and recreated
--       with the identical body plus the one new column in the compare.

ALTER TABLE space_zones ADD COLUMN icon TEXT;

DROP TRIGGER IF EXISTS space_zones_sync_seq_update;
CREATE TRIGGER space_zones_sync_seq_update
AFTER UPDATE ON space_zones
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.name, NEW.latitude, NEW.longitude, NEW.radius_m, NEW.color, NEW.icon, NEW.created_by, NEW.created_at, NEW.deleted_at, NEW.deleted_by)
   IS NOT (OLD.id, OLD.space_id, OLD.name, OLD.latitude, OLD.longitude, OLD.radius_m, OLD.color, OLD.icon, OLD.created_by, OLD.created_at, OLD.deleted_at, OLD.deleted_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_zones SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

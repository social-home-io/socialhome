-- 0104 — a space event's RSVP mirror keeps the event's time zone.
-- Follows 0103 (space feed read marker).
--
-- Going to a space event mirrors it onto the member's household calendar
-- (``calendar_events.mirrored_from`` → ``space_calendar_events.id``,
-- ``SpaceRsvpMirrorBridge``). The bridge copied the bounds but not ``tz``,
-- so every mirror carried the column default ``'UTC'``. An all-day event is
-- stored as its zone's midnights (Zurich: ``23:00Z`` – ``22:59Z``) and read
-- in that zone — read in UTC the mirror started a day early: the household
-- calendar said "Sun 8 – Sat 14" for a Mon 9 – Sat 14 holiday the space
-- calendar showed right. The bridge now copies ``tz``; this migration
--
--   (a) repairs the mirrors already on disk: ``tz``, ``start_dt``,
--       ``end_dt`` and ``all_day`` are set to the live source event's (a
--       mirror is a copy of it — rows minted by older code also carried
--       stale bounds); a mirror whose source is gone or tombstoned is left
--       alone, and
--   (b) installs BEFORE INSERT / BEFORE UPDATE triggers that refuse a mirror
--       whose ``tz`` differs from its live source event's, so no writer (a
--       regression, a restored backup) can store one again.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every writer of ``calendar_events.mirrored_from`` / ``tz``:
--       ``SpaceRsvpMirrorBridge`` (the only producer of mirrors — upsert on a
--       going RSVP, refresh on a source update, delete on decline / leave /
--       source delete); ``calendar_repo.save_event`` (named-column upsert);
--       the household calendar create / update (never sets
--       ``mirrored_from``); ``backup_service`` (whole-row restore — the
--       triggers now refuse a restored mismatch, the repair above already
--       ran on the live rows). The federation paths that read a
--       ``mirrored_from`` field build *space* events
--       (``space_calendar_events``), not household mirrors.
--   (2) Non-migration alternatives considered and rejected: reading the
--       source's ``tz`` at read time (every household-calendar reader would
--       have to join the space table, and the ICS export / dashboard /
--       reminders would each need it); fixing only the bridge (existing
--       mirrors stay a day off until their source is edited — most never
--       are); dropping ``tz`` from mirrors and normalising their bounds to
--       UTC midnights (changes what the mirror stores, loses the zone).
--   (3) Smallest change: an UPDATE of only the mirrors that differ from
--       their live source, plus two triggers. No column, no index, no table
--       rebuild.

UPDATE calendar_events
   SET tz = (SELECT COALESCE(s.tz, 'UTC') FROM space_calendar_events s
              WHERE s.id = calendar_events.mirrored_from),
       start_dt = (SELECT s.start_dt FROM space_calendar_events s
                    WHERE s.id = calendar_events.mirrored_from),
       end_dt = (SELECT s.end_dt FROM space_calendar_events s
                  WHERE s.id = calendar_events.mirrored_from),
       all_day = (SELECT s.all_day FROM space_calendar_events s
                   WHERE s.id = calendar_events.mirrored_from)
 WHERE mirrored_from IS NOT NULL
   AND EXISTS (
       SELECT 1 FROM space_calendar_events s
        WHERE s.id = calendar_events.mirrored_from
          AND s.deleted_at IS NULL
          AND s.start_dt != ''
          AND (COALESCE(s.tz, 'UTC') IS NOT COALESCE(calendar_events.tz, 'UTC')
               OR s.start_dt IS NOT calendar_events.start_dt
               OR s.end_dt IS NOT calendar_events.end_dt
               OR s.all_day IS NOT calendar_events.all_day)
   );

CREATE TRIGGER IF NOT EXISTS calendar_events_mirror_tz_insert
BEFORE INSERT ON calendar_events
WHEN NEW.mirrored_from IS NOT NULL
 AND EXISTS (
     SELECT 1 FROM space_calendar_events s
      WHERE s.id = NEW.mirrored_from
        AND s.deleted_at IS NULL
        AND COALESCE(s.tz, 'UTC') IS NOT COALESCE(NEW.tz, 'UTC')
 )
BEGIN
    SELECT RAISE(ABORT, 'a space event mirror must keep the event''s tz');
END;

CREATE TRIGGER IF NOT EXISTS calendar_events_mirror_tz_update
BEFORE UPDATE OF tz, mirrored_from ON calendar_events
WHEN NEW.mirrored_from IS NOT NULL
 AND EXISTS (
     SELECT 1 FROM space_calendar_events s
      WHERE s.id = NEW.mirrored_from
        AND s.deleted_at IS NULL
        AND COALESCE(s.tz, 'UTC') IS NOT COALESCE(NEW.tz, 'UTC')
 )
BEGIN
    SELECT RAISE(ABORT, 'a space event mirror must keep the event''s tz');
END;

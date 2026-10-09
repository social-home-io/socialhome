-- 0085 — Space stickies, calendar events, gallery albums / items and zones
-- keep a tombstone (§25.6 sync); a deleted space post holds no poll.
--
-- 1. ``deleted_at`` / ``deleted_by`` on ``stickies``, ``space_calendar_events``,
--    ``gallery_albums``, ``gallery_items`` and ``space_zones`` — the in-row
--    tombstone shape of tasks (0069 / 0071) and pages (0073). A delete of a
--    space row keeps the row, its content blanked, instead of removing it.
--    Without it a household that missed the live ``*_DELETED`` event
--    (offline past the outbox, a mesh drop) kept the row forever and, as a
--    §25.6 catch-up provider, re-spread it to every joiner. The host now
--    streams the tombstones (``stickies_deleted``, ``calendar_deleted``,
--    ``gallery_albums_deleted``, ``gallery_items_deleted``,
--    ``space_zones_deleted``) and no upsert brings a tombstoned id back.
--    ``deleted_by`` names the user who made the delete — shipped as
--    ``actor_user_id`` so a receiver judges it against the space's
--    ``stickies`` / ``calendar`` access level like the live delete. NULL =
--    live, so every existing row stays live. Household rows
--    (``stickies.space_id`` / ``gallery_albums.space_id`` NULL) never
--    federate and are still deleted outright.
-- 2. Trigger ``space_calendar_events_tombstone_drops_rsvps`` — a tombstoned
--    event's RSVPs and RSVP reminders go (neither table has a FK to the
--    event, so the old hard delete orphaned them).
-- 3. Trigger ``gallery_albums_tombstone_drops_items`` — an album tombstone
--    tombstones its live items IN PLACE (files blanked, so the media
--    reference count drops them and the delete path unlinks the blobs), the
--    0071 list → task shape. The old ``ON DELETE CASCADE`` removed every
--    trace of the item ids.
-- 4. Trigger ``space_posts_delete_drops_polls`` + four ``BEFORE INSERT``
--    guards — a post soft-delete (``deleted`` 0 → 1: the author, a
--    moderator, the retention sweep, a live ``SPACE_POST_DELETED``, a
--    ``posts_deleted`` tombstone) drops the post's reply poll (options,
--    votes) and schedule poll (meta, slots, responses); a deleted post takes
--    no new one. ``ON DELETE CASCADE`` to ``space_posts`` never fired: the
--    post row is kept as its tombstone. Then a repair: the poll / schedule
--    rows already left under deleted posts are removed.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data.
--       ``grep -rn "stickies\|space_calendar_events\|gallery_items\|
--       gallery_albums\|space_zones" socialhome``: all SQL lives in
--       ``sticky_repo`` / ``calendar_repo`` (SqliteSpaceCalendarRepo) /
--       ``gallery_repo`` / ``space_zone_repo``, plus the whole-table backup
--       dump and the per-user data export (copy the new columns as-is) and
--       the media reference scan (reads ``gallery_items.filename`` /
--       ``thumbnail_filename`` and ``space_calendar_events.cover_url`` —
--       a tombstone blanks them, so a deleted row references no file). Every
--       live read in those repos now skips tombstones, so every service,
--       route, exporter, the resume provider and the federation inbound
--       handlers above them see a deleted row as absent — as the hard
--       delete left it. Every upsert refuses a tombstone (``ON CONFLICT …
--       WHERE deleted_at IS NULL`` / the scoped existence checks). The
--       delete sites: ``StickyService.delete`` + ``_on_sticky_deleted``;
--       ``CalendarService.delete_event`` + ``_on_calendar_deleted``;
--       ``GalleryService.delete_item`` / ``delete_album`` +
--       ``_on_gallery_item_deleted`` / ``_on_gallery_album_deleted``;
--       ``SpaceZoneService.delete_zone`` + ``_on_zone_deleted``. Space album
--       deletes were remembered only in memory
--       (``GalleryAlbumTombstones``, forgotten on restart, never streamed);
--       the tombstone row replaces it. Poll rows: written by
--       ``SqliteSpacePollRepo`` only; space posts are only ever soft-deleted
--       (``SqliteSpacePostRepo.soft_delete`` / a ``deleted=1`` stub), which
--       no FK cascade sees.
--   (2) Non-migration alternatives considered and rejected:
--       * One shared ``space_content_tombstones(kind, id, …)`` table — a
--         second home for each id; the one PRIMARY KEY's ON CONFLICT is
--         what makes "a tombstone wins" a single atomic statement (the 0073
--         reasoning), and 0069 / 0071 / 0073 set the in-row shape.
--       * Keeping the in-memory album record — forgets on restart, cannot
--         be streamed to a household offline across the delete.
--       * A ``deleted`` flag on the live sync records — an older receiver
--         would read a tombstone as a live row; separate resources are
--         dropped as unknown instead.
--       * Deleting poll rows in the service delete paths — five write sites
--         (local, live inbound, sync tombstone, retention sweep, moderation)
--         and any future one would have to remember; the trigger makes "a
--         deleted post holds no poll" a database invariant.
--   (3) Smallest possible change: ten NULL-default columns (metadata-only in
--       SQLite — no table rewrite, no backfill), no new index (tombstone
--       reads are per space, served by the existing space indexes; gallery
--       items join their album), triggers that fire only on a later live →
--       deleted transition or insert. The one change to existing rows is
--       the repair in (4): poll / schedule rows of posts ALREADY deleted —
--       content of a deleted post no read path may show.

ALTER TABLE stickies ADD COLUMN deleted_at TEXT;
ALTER TABLE stickies ADD COLUMN deleted_by TEXT;
ALTER TABLE space_calendar_events ADD COLUMN deleted_at TEXT;
ALTER TABLE space_calendar_events ADD COLUMN deleted_by TEXT;
ALTER TABLE gallery_albums ADD COLUMN deleted_at TEXT;
ALTER TABLE gallery_albums ADD COLUMN deleted_by TEXT;
ALTER TABLE gallery_items ADD COLUMN deleted_at TEXT;
ALTER TABLE gallery_items ADD COLUMN deleted_by TEXT;
ALTER TABLE space_zones ADD COLUMN deleted_at TEXT;
ALTER TABLE space_zones ADD COLUMN deleted_by TEXT;

CREATE TRIGGER space_calendar_events_tombstone_drops_rsvps
AFTER UPDATE OF deleted_at ON space_calendar_events
WHEN NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL
BEGIN
    DELETE FROM space_calendar_rsvps WHERE event_id = NEW.id;
    DELETE FROM space_calendar_rsvp_reminders WHERE event_id = NEW.id;
END;

CREATE TRIGGER gallery_albums_tombstone_drops_items
AFTER UPDATE OF deleted_at ON gallery_albums
WHEN NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL
BEGIN
    UPDATE gallery_items
       SET deleted_at = NEW.deleted_at,
           deleted_by = NEW.deleted_by,
           filename = '',
           thumbnail_filename = '',
           width = 0,
           height = 0,
           duration_s = NULL,
           caption = NULL,
           taken_at = NULL
     WHERE album_id = NEW.id AND deleted_at IS NULL;
END;

CREATE TRIGGER space_posts_delete_drops_polls
AFTER UPDATE OF deleted ON space_posts
WHEN NEW.deleted = 1 AND OLD.deleted = 0
BEGIN
    DELETE FROM space_poll_votes WHERE option_id IN
        (SELECT id FROM space_poll_options WHERE post_id = NEW.id);
    DELETE FROM space_poll_options WHERE post_id = NEW.id;
    DELETE FROM space_polls WHERE post_id = NEW.id;
    DELETE FROM space_schedule_responses WHERE slot_id IN
        (SELECT id FROM space_schedule_slots WHERE post_id = NEW.id);
    DELETE FROM space_schedule_slots WHERE post_id = NEW.id;
    DELETE FROM space_schedule_poll_meta WHERE post_id = NEW.id;
END;

CREATE TRIGGER space_polls_skip_deleted_post
BEFORE INSERT ON space_polls
WHEN EXISTS (SELECT 1 FROM space_posts WHERE id = NEW.post_id AND deleted = 1)
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER space_poll_options_skip_deleted_post
BEFORE INSERT ON space_poll_options
WHEN EXISTS (SELECT 1 FROM space_posts WHERE id = NEW.post_id AND deleted = 1)
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER space_schedule_poll_meta_skip_deleted_post
BEFORE INSERT ON space_schedule_poll_meta
WHEN EXISTS (SELECT 1 FROM space_posts WHERE id = NEW.post_id AND deleted = 1)
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER space_schedule_slots_skip_deleted_post
BEFORE INSERT ON space_schedule_slots
WHEN EXISTS (SELECT 1 FROM space_posts WHERE id = NEW.post_id AND deleted = 1)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Repair: the poll / schedule rows already left under deleted posts.
DELETE FROM space_poll_votes WHERE option_id IN (
    SELECT o.id FROM space_poll_options o
      JOIN space_posts p ON p.id = o.post_id
     WHERE p.deleted = 1
);
DELETE FROM space_poll_options
 WHERE post_id IN (SELECT id FROM space_posts WHERE deleted = 1);
DELETE FROM space_polls
 WHERE post_id IN (SELECT id FROM space_posts WHERE deleted = 1);
DELETE FROM space_schedule_responses WHERE slot_id IN (
    SELECT s.id FROM space_schedule_slots s
      JOIN space_posts p ON p.id = s.post_id
     WHERE p.deleted = 1
);
DELETE FROM space_schedule_slots
 WHERE post_id IN (SELECT id FROM space_posts WHERE deleted = 1);
DELETE FROM space_schedule_poll_meta
 WHERE post_id IN (SELECT id FROM space_posts WHERE deleted = 1);

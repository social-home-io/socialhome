-- 0086 — §25.6 incremental space sync: a per-row change stamp
-- (``sync_seq``) on the history-growing synced tables, maintained by
-- triggers from one per-household counter, and a per-household watermark
-- on ``space_instances``. A periodic (``"incremental"``) sync session then
-- streams only the rows changed since the last stream the household
-- confirmed, instead of re-reading and re-encrypting the whole retention
-- window for every peer × shared space every 30 minutes.
--
-- 1. ``sync_seq_counter`` — one row, a monotonic per-household sequence.
--    A counter, not a wall clock: no clock skew, no mixed naive / tz-aware
--    ``created_at`` forms, no two changes in one second sharing a stamp.
-- 2. ``sync_seq INTEGER`` (NULL default) on ``space_posts``,
--    ``space_post_comments``, ``conversation_messages``, ``gallery_albums``,
--    ``gallery_items``, ``space_calendar_events``, ``stickies``,
--    ``space_zones``. An ``AFTER INSERT`` trigger and an ``AFTER UPDATE``
--    trigger stamp the row from the counter. The update trigger fires only
--    when a synced column actually changes (row-value ``IS NOT``) — so a
--    receiver re-applying a streamed row it already holds stamps nothing and
--    two households never bounce a row back and forth — or when the stamp
--    is set to NULL (a "touch", below). Left out of the comparison: local
--    bookkeeping that an idempotent apply may rewrite (``updated_at``, an
--    event's ``notified_at``, a message's ``media_sync_status``); a real
--    edit always changes a content column too. Python never writes
--    ``sync_seq``: every write path — repos, federation inbound, the
--    retention sweep, moderation, the 0085 tombstone triggers — stamps by
--    construction. Existing rows stay NULL: no backfill. A household's first
--    sync after the upgrade has no watermark and so streams in full.
-- 3. Touch triggers: a post's reply poll (``space_polls``,
--    ``space_poll_options``, ``space_poll_votes``), schedule poll
--    (``space_schedule_poll_meta``, ``space_schedule_slots``) and bazaar
--    listing (``bazaar_listings``) are streamed keyed by the post
--    (``polls`` / ``schedules`` / ``bazaar`` walk the changed posts), so a
--    change to one of them sets the post's ``sync_seq`` to NULL and the
--    post's update trigger re-stamps it. Schedule responses and bazaar bids
--    / offers are not part of any synced record and stamp nothing.
-- 4. Partial indexes on ``sync_seq`` for the tables that grow with history
--    (posts, comments, chat messages, gallery items). The small per-space
--    tables are served by their existing space indexes.
-- 5. ``space_instances.synced_seq`` / ``synced_shape`` / ``synced_full_at``
--    — the provider's watermark for the (space, member household): the
--    counter snapshot of the last stream that household confirmed
--    (``SPACE_SYNC_COMPLETE``), the session shape it was taken under, and
--    when its last full stream completed. NULL = none → a full stream.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data.
--       ``grep -rn "space_posts\|space_post_comments\|conversation_messages\|
--       gallery_albums\|gallery_items\|space_calendar_events\|stickies\|
--       space_zones\|space_poll\|space_schedule\|bazaar_listings" socialhome``:
--       writes live in ``space_post_repo``, ``conversation_repo``,
--       ``gallery_repo``, ``calendar_repo`` (SqliteSpaceCalendarRepo),
--       ``sticky_repo``, ``space_zone_repo``, ``space_poll_repo``,
--       ``bazaar_repo`` and the 0085 / ``space_posts_delete_drops_polls``
--       triggers, reached from the local services, every federation inbound
--       handler, the §25.6 receiver, the retention sweep and moderation —
--       too many write sites to stamp by hand, hence triggers. No covered
--       table is written with ``INSERT OR REPLACE`` (which would re-insert,
--       and stamp, on an idempotent apply). The §25.6 exporters read these
--       tables through keyset ``*_sync_page`` / ``*_tombstones_page`` repo
--       methods, which gain a ``since`` filter. ``space_instances`` is
--       written by ``space_repo.add_space_instance`` (an ``ON CONFLICT DO
--       UPDATE`` upsert that leaves the new columns alone) and
--       ``remove_space_instance`` (a DELETE — the watermark goes with the
--       seat, so a household that rejoins syncs in full) and by
--       ``federation_repo`` (version claims); the sync manager's BEGIN
--       admission already requires the row. The backup export copies none
--       of the stamped tables' counters or ``space_instances`` (a restore
--       re-pairs under a new identity: no watermark survives it), and the
--       restored rows are stamped fresh by the insert triggers.
--   (2) Non-migration alternatives considered and rejected:
--       * Computing "changed" at read time from existing columns — there is
--         none that covers it: ``created_at`` misses every edit, ``edited_at``
--         misses reactions, votes, moderation and soft-deletes, and
--         ``updated_at`` exists on four tables only and is wall-clock.
--       * A per-space change-log table (``(space, resource, row) → seq``) —
--         a second home for every synced row id, joined back on every read
--         and pruned forever; the in-row column is one predicate on the
--         keyset reads the exporters already run.
--       * Stamping in the repos — dozens of write sites across eight repos,
--         every future one would have to remember, and a missed one silently
--         drops a change from every incremental stream; a trigger is a
--         database invariant (the 0085 reasoning).
--       * Keeping the watermark in memory (as ``_mesh_catchup_done``) — lost
--         on every restart, so every boot would full-sync every space; or
--         on the requester, sent in the BEGIN — a wire change (a protocol
--         bump), and a watermark in the provider's counter space is only
--         meaningful to the provider.
--       * A new ``space_sync_watermarks`` table — ``space_instances`` already
--         is the (space, member household) row a BEGIN is admitted against,
--         and its lifetime (dropped with the household's last seat) is the
--         watermark's.
--   (3) Smallest possible change: one one-row table, eleven NULL-default
--       columns (metadata-only in SQLite — no table rewrite, no backfill),
--       four partial indexes (empty until rows change), triggers. Nothing
--       existing is rewritten or dropped.

CREATE TABLE sync_seq_counter (
    id   INTEGER PRIMARY KEY CHECK(id = 1),
    seq  INTEGER NOT NULL DEFAULT 0
);
INSERT INTO sync_seq_counter(id, seq) VALUES(1, 0);

ALTER TABLE space_posts ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER space_posts_sync_seq_insert
AFTER INSERT ON space_posts
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_posts SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER space_posts_sync_seq_update
AFTER UPDATE ON space_posts
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.author, NEW.bot_id, NEW.linked_event_id, NEW.type, NEW.content, NEW.media_url, NEW.reactions, NEW.comment_count, NEW.pinned, NEW.deleted, NEW.edited_at, NEW.no_link_preview, NEW.moderated, NEW.file_meta_json, NEW.location_json, NEW.image_urls_json, NEW.linked_highlight_id, NEW.created_at, NEW.hidden_from_feed, NEW.link_preview_json, NEW.reaction_stamps_json, NEW.moderated_by)
   IS NOT (OLD.id, OLD.space_id, OLD.author, OLD.bot_id, OLD.linked_event_id, OLD.type, OLD.content, OLD.media_url, OLD.reactions, OLD.comment_count, OLD.pinned, OLD.deleted, OLD.edited_at, OLD.no_link_preview, OLD.moderated, OLD.file_meta_json, OLD.location_json, OLD.image_urls_json, OLD.linked_highlight_id, OLD.created_at, OLD.hidden_from_feed, OLD.link_preview_json, OLD.reaction_stamps_json, OLD.moderated_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_posts SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE space_post_comments ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER space_post_comments_sync_seq_insert
AFTER INSERT ON space_post_comments
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_post_comments SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER space_post_comments_sync_seq_update
AFTER UPDATE ON space_post_comments
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.post_id, NEW.parent_id, NEW.author, NEW.type, NEW.content, NEW.media_url, NEW.deleted, NEW.edited_at, NEW.created_at)
   IS NOT (OLD.id, OLD.post_id, OLD.parent_id, OLD.author, OLD.type, OLD.content, OLD.media_url, OLD.deleted, OLD.edited_at, OLD.created_at))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_post_comments SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE conversation_messages ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER conversation_messages_sync_seq_insert
AFTER INSERT ON conversation_messages
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE conversation_messages SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER conversation_messages_sync_seq_update
AFTER UPDATE ON conversation_messages
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.conversation_id, NEW.sender_user_id, NEW.content, NEW.type, NEW.media_url, NEW.reply_to_id, NEW.reply_to_highlight_frame_id, NEW.reply_to_highlight_frame_snapshot, NEW.deleted, NEW.edited_at, NEW.created_at, NEW.file_name, NEW.mime_type, NEW.file_size_bytes, NEW.media_blob_id)
   IS NOT (OLD.id, OLD.conversation_id, OLD.sender_user_id, OLD.content, OLD.type, OLD.media_url, OLD.reply_to_id, OLD.reply_to_highlight_frame_id, OLD.reply_to_highlight_frame_snapshot, OLD.deleted, OLD.edited_at, OLD.created_at, OLD.file_name, OLD.mime_type, OLD.file_size_bytes, OLD.media_blob_id))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE conversation_messages SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE gallery_albums ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER gallery_albums_sync_seq_insert
AFTER INSERT ON gallery_albums
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE gallery_albums SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER gallery_albums_sync_seq_update
AFTER UPDATE ON gallery_albums
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.retention_exempt, NEW.is_system, NEW.owner_user_id, NEW.name, NEW.description, NEW.cover_item_id, NEW.item_count, NEW.created_at, NEW.deleted_at, NEW.deleted_by)
   IS NOT (OLD.id, OLD.space_id, OLD.retention_exempt, OLD.is_system, OLD.owner_user_id, OLD.name, OLD.description, OLD.cover_item_id, OLD.item_count, OLD.created_at, OLD.deleted_at, OLD.deleted_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE gallery_albums SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE gallery_items ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER gallery_items_sync_seq_insert
AFTER INSERT ON gallery_items
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE gallery_items SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER gallery_items_sync_seq_update
AFTER UPDATE ON gallery_items
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.album_id, NEW.uploaded_by, NEW.item_type, NEW.filename, NEW.thumbnail_filename, NEW.width, NEW.height, NEW.duration_s, NEW.caption, NEW.taken_at, NEW.sort_order, NEW.source_post_id, NEW.created_at, NEW.deleted_at, NEW.deleted_by)
   IS NOT (OLD.id, OLD.album_id, OLD.uploaded_by, OLD.item_type, OLD.filename, OLD.thumbnail_filename, OLD.width, OLD.height, OLD.duration_s, OLD.caption, OLD.taken_at, OLD.sort_order, OLD.source_post_id, OLD.created_at, OLD.deleted_at, OLD.deleted_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE gallery_items SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE space_calendar_events ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER space_calendar_events_sync_seq_insert
AFTER INSERT ON space_calendar_events
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_calendar_events SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER space_calendar_events_sync_seq_update
AFTER UPDATE ON space_calendar_events
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.summary, NEW.description, NEW.start_dt, NEW.end_dt, NEW.all_day, NEW.attendees_json, NEW.rrule, NEW.created_by, NEW.capacity, NEW.notify_before_minutes, NEW.cover_url, NEW.location, NEW.created_at, NEW.tz, NEW.announce_in_feed, NEW.deleted_at, NEW.deleted_by)
   IS NOT (OLD.id, OLD.space_id, OLD.summary, OLD.description, OLD.start_dt, OLD.end_dt, OLD.all_day, OLD.attendees_json, OLD.rrule, OLD.created_by, OLD.capacity, OLD.notify_before_minutes, OLD.cover_url, OLD.location, OLD.created_at, OLD.tz, OLD.announce_in_feed, OLD.deleted_at, OLD.deleted_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_calendar_events SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE stickies ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER stickies_sync_seq_insert
AFTER INSERT ON stickies
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE stickies SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER stickies_sync_seq_update
AFTER UPDATE ON stickies
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.author, NEW.content, NEW.color, NEW.position_x, NEW.position_y, NEW.created_at, NEW.deleted_at, NEW.deleted_by)
   IS NOT (OLD.id, OLD.space_id, OLD.author, OLD.content, OLD.color, OLD.position_x, OLD.position_y, OLD.created_at, OLD.deleted_at, OLD.deleted_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE stickies SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

ALTER TABLE space_zones ADD COLUMN sync_seq INTEGER;
CREATE TRIGGER space_zones_sync_seq_insert
AFTER INSERT ON space_zones
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_zones SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;
CREATE TRIGGER space_zones_sync_seq_update
AFTER UPDATE ON space_zones
WHEN NEW.sync_seq IS NULL
  OR (NEW.sync_seq IS OLD.sync_seq
      AND (NEW.id, NEW.space_id, NEW.name, NEW.latitude, NEW.longitude, NEW.radius_m, NEW.color, NEW.created_by, NEW.created_at, NEW.deleted_at, NEW.deleted_by)
   IS NOT (OLD.id, OLD.space_id, OLD.name, OLD.latitude, OLD.longitude, OLD.radius_m, OLD.color, OLD.created_by, OLD.created_at, OLD.deleted_at, OLD.deleted_by))
BEGIN
    UPDATE sync_seq_counter SET seq = seq + 1 WHERE id = 1;
    UPDATE space_zones SET sync_seq = (SELECT seq FROM sync_seq_counter WHERE id = 1)
     WHERE id = NEW.id;
END;

CREATE TRIGGER space_polls_touch_post_insert
AFTER INSERT ON space_polls
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
END;
CREATE TRIGGER space_polls_touch_post_update
AFTER UPDATE ON space_polls
WHEN (NEW.post_id, NEW.question, NEW.closes_at, NEW.closed, NEW.allow_multiple)
 IS NOT (OLD.post_id, OLD.question, OLD.closes_at, OLD.closed, OLD.allow_multiple)
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id AND OLD.post_id IS NOT NEW.post_id;
END;
CREATE TRIGGER space_polls_touch_post_delete
AFTER DELETE ON space_polls
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id;
END;

CREATE TRIGGER space_poll_options_touch_post_insert
AFTER INSERT ON space_poll_options
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
END;
CREATE TRIGGER space_poll_options_touch_post_update
AFTER UPDATE ON space_poll_options
WHEN (NEW.id, NEW.post_id, NEW.text, NEW.position)
 IS NOT (OLD.id, OLD.post_id, OLD.text, OLD.position)
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id AND OLD.post_id IS NOT NEW.post_id;
END;
CREATE TRIGGER space_poll_options_touch_post_delete
AFTER DELETE ON space_poll_options
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id;
END;

CREATE TRIGGER space_schedule_poll_meta_touch_post_insert
AFTER INSERT ON space_schedule_poll_meta
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
END;
CREATE TRIGGER space_schedule_poll_meta_touch_post_update
AFTER UPDATE ON space_schedule_poll_meta
WHEN (NEW.post_id, NEW.title, NEW.deadline, NEW.finalized_slot_id, NEW.closed)
 IS NOT (OLD.post_id, OLD.title, OLD.deadline, OLD.finalized_slot_id, OLD.closed)
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id AND OLD.post_id IS NOT NEW.post_id;
END;
CREATE TRIGGER space_schedule_poll_meta_touch_post_delete
AFTER DELETE ON space_schedule_poll_meta
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id;
END;

CREATE TRIGGER space_schedule_slots_touch_post_insert
AFTER INSERT ON space_schedule_slots
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
END;
CREATE TRIGGER space_schedule_slots_touch_post_update
AFTER UPDATE ON space_schedule_slots
WHEN (NEW.id, NEW.post_id, NEW.slot_date, NEW.start_time, NEW.end_time, NEW.position)
 IS NOT (OLD.id, OLD.post_id, OLD.slot_date, OLD.start_time, OLD.end_time, OLD.position)
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id AND OLD.post_id IS NOT NEW.post_id;
END;
CREATE TRIGGER space_schedule_slots_touch_post_delete
AFTER DELETE ON space_schedule_slots
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id;
END;

CREATE TRIGGER bazaar_listings_touch_post_insert
AFTER INSERT ON bazaar_listings
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
END;
CREATE TRIGGER bazaar_listings_touch_post_update
AFTER UPDATE ON bazaar_listings
WHEN (NEW.post_id, NEW.space_id, NEW.seller_user_id, NEW.mode, NEW.title, NEW.description, NEW.image_urls_json, NEW.end_time, NEW.currency, NEW.status, NEW.price, NEW.start_price, NEW.step_price, NEW.winner_user_id, NEW.winning_price, NEW.sold_at, NEW.created_at)
 IS NOT (OLD.post_id, OLD.space_id, OLD.seller_user_id, OLD.mode, OLD.title, OLD.description, OLD.image_urls_json, OLD.end_time, OLD.currency, OLD.status, OLD.price, OLD.start_price, OLD.step_price, OLD.winner_user_id, OLD.winning_price, OLD.sold_at, OLD.created_at)
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id AND OLD.post_id IS NOT NEW.post_id;
END;
CREATE TRIGGER bazaar_listings_touch_post_delete
AFTER DELETE ON bazaar_listings
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id;
END;

CREATE TRIGGER space_poll_votes_touch_post_insert
AFTER INSERT ON space_poll_votes
BEGIN
    UPDATE space_posts SET sync_seq = NULL
     WHERE id = (SELECT post_id FROM space_poll_options WHERE id = NEW.option_id);
END;
CREATE TRIGGER space_poll_votes_touch_post_delete
AFTER DELETE ON space_poll_votes
BEGIN
    UPDATE space_posts SET sync_seq = NULL
     WHERE id = (SELECT post_id FROM space_poll_options WHERE id = OLD.option_id);
END;

CREATE INDEX idx_space_posts_sync_seq
    ON space_posts(space_id, sync_seq) WHERE sync_seq IS NOT NULL;
CREATE INDEX idx_space_post_comments_sync_seq
    ON space_post_comments(sync_seq) WHERE sync_seq IS NOT NULL;
CREATE INDEX idx_conversation_messages_sync_seq
    ON conversation_messages(conversation_id, sync_seq) WHERE sync_seq IS NOT NULL;
CREATE INDEX idx_gallery_items_sync_seq
    ON gallery_items(sync_seq) WHERE sync_seq IS NOT NULL;

ALTER TABLE space_instances ADD COLUMN synced_seq INTEGER;
ALTER TABLE space_instances ADD COLUMN synced_shape TEXT;
ALTER TABLE space_instances ADD COLUMN synced_full_at TEXT;

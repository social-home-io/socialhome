-- 0146 — one merge rule for changes to existing space items (v_94).
--
-- 1. ``space_polls.vote_stamps_json`` (NULL default) — per voter, the
--    stamp of their selection: ``{voter: [stamp, [option_id, …]]}``. A
--    retraction is an entry with no options — the tombstone that a late
--    copy of the vote it took back cannot get past. ``space_poll_votes``
--    keeps holding the selection (the tallies read it, unchanged).
-- 2. Touch triggers, the 0086 pattern: a schedule answer
--    (``space_schedule_responses``), a bazaar bid (``bazaar_bids``) and an
--    RSVP (``space_calendar_rsvps``) are part of their post's / event's
--    §25.6 record since v_92 but stamped nothing, so a missed one healed
--    only on the host's daily full stream. Now a change to one sets the
--    parent's ``sync_seq`` to NULL (re-stamped by the parent's own 0086
--    trigger) and the 30-minute incremental session carries it. The update
--    triggers compare only what a record carries and a re-apply cannot
--    rewrite (an answer's ``availability``, an RSVP's ``status``, a bid's
--    amount / message / state — not ``responded_at`` / ``updated_at``,
--    which a re-delivered live event rewrites, nor a bid's
--    ``rejection_reason`` / ``created_at``, which never stream), so
--    applying a record a household already holds stamps nothing and two
--    households never bounce a row.
-- 3. ``space_polls_touch_post_update`` is recreated with the new column,
--    so a stamp-only vote change re-streams the poll.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data.
--       ``space_polls`` / ``space_poll_votes``: ``space_poll_repo`` (the
--       local ``PollService`` writes, ``set_votes_in_space`` /
--       ``close_in_space`` for federation inbound, ``apply_synced_poll``
--       for the §25.6 receiver, ``list_votes`` for the exporter),
--       ``poll_deadline_scheduler`` (through ``close_if_open``), the 0085
--       ``space_posts_delete_drops_polls`` trigger, the backup / data
--       export (whole-table dumps: the new columns go with the row).
--       ``space_schedule_responses``: ``space_poll_repo`` only.
--       ``space_calendar_rsvps``: ``calendar_repo`` (SqliteSpaceCalendarRepo)
--       and the 0085 event-tombstone trigger. ``bazaar_bids``:
--       ``bazaar_repo`` only. None is written with ``INSERT OR REPLACE``.
--   (2) Non-migration alternatives considered and rejected:
--       * ``space_poll_votes.voted_at`` as the stamp — it is on the vote
--         row, so a retraction (the rows are deleted) leaves no stamp and
--         a late older vote comes back; and it is the arrival time, not
--         the voter's.
--       * A stamp in ``space_posts.reaction_stamps_json`` (migration 0075)
--         under a reserved key — one column, two unrelated meanings, and
--         every reaction read / cap / eviction would have to skip it.
--       * Ordering in the outbox only (#1353's latest-only key) — it
--         cannot recall a retry already in flight, nor order two
--         transports, nor a close.
--       * For 2.: stamping the parent from Python — every write site would
--         have to remember (the 0086 reasoning); or a full stream more
--         often — the whole window for every space instead of one record.
--   (3) Smallest possible change: one NULL-default column (metadata-only
--       in SQLite: no rewrite, no backfill — a vote from before has no
--       stamp and follows the host's list as before, until its voter
--       votes again), nine touch triggers and one trigger recreated.
--       Nothing existing is rewritten or dropped.

ALTER TABLE space_polls ADD COLUMN vote_stamps_json TEXT;

DROP TRIGGER space_polls_touch_post_update;
CREATE TRIGGER space_polls_touch_post_update
AFTER UPDATE ON space_polls
WHEN (NEW.post_id, NEW.question, NEW.closes_at, NEW.closed, NEW.allow_multiple, NEW.vote_stamps_json)
 IS NOT (OLD.post_id, OLD.question, OLD.closes_at, OLD.closed, OLD.allow_multiple, OLD.vote_stamps_json)
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.post_id;
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.post_id AND OLD.post_id IS NOT NEW.post_id;
END;

CREATE TRIGGER space_schedule_responses_touch_post_insert
AFTER INSERT ON space_schedule_responses
BEGIN
    UPDATE space_posts SET sync_seq = NULL
     WHERE id = (SELECT post_id FROM space_schedule_slots WHERE id = NEW.slot_id);
END;
CREATE TRIGGER space_schedule_responses_touch_post_update
AFTER UPDATE ON space_schedule_responses
WHEN (NEW.slot_id, NEW.user_id, NEW.availability)
 IS NOT (OLD.slot_id, OLD.user_id, OLD.availability)
BEGIN
    UPDATE space_posts SET sync_seq = NULL
     WHERE id IN (SELECT post_id FROM space_schedule_slots WHERE id IN (NEW.slot_id, OLD.slot_id));
END;
CREATE TRIGGER space_schedule_responses_touch_post_delete
AFTER DELETE ON space_schedule_responses
BEGIN
    UPDATE space_posts SET sync_seq = NULL
     WHERE id = (SELECT post_id FROM space_schedule_slots WHERE id = OLD.slot_id);
END;

CREATE TRIGGER bazaar_bids_touch_post_insert
AFTER INSERT ON bazaar_bids
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = NEW.listing_post_id;
END;
CREATE TRIGGER bazaar_bids_touch_post_update
AFTER UPDATE ON bazaar_bids
WHEN (NEW.id, NEW.listing_post_id, NEW.bidder_user_id, NEW.amount, NEW.message, NEW.accepted, NEW.rejected, NEW.withdrawn)
 IS NOT (OLD.id, OLD.listing_post_id, OLD.bidder_user_id, OLD.amount, OLD.message, OLD.accepted, OLD.rejected, OLD.withdrawn)
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id IN (NEW.listing_post_id, OLD.listing_post_id);
END;
CREATE TRIGGER bazaar_bids_touch_post_delete
AFTER DELETE ON bazaar_bids
BEGIN
    UPDATE space_posts SET sync_seq = NULL WHERE id = OLD.listing_post_id;
END;

CREATE TRIGGER space_calendar_rsvps_touch_event_insert
AFTER INSERT ON space_calendar_rsvps
BEGIN
    UPDATE space_calendar_events SET sync_seq = NULL WHERE id = NEW.event_id;
END;
CREATE TRIGGER space_calendar_rsvps_touch_event_update
AFTER UPDATE ON space_calendar_rsvps
WHEN (NEW.event_id, NEW.user_id, NEW.occurrence_at, NEW.status)
 IS NOT (OLD.event_id, OLD.user_id, OLD.occurrence_at, OLD.status)
BEGIN
    UPDATE space_calendar_events SET sync_seq = NULL WHERE id IN (NEW.event_id, OLD.event_id);
END;
CREATE TRIGGER space_calendar_rsvps_touch_event_delete
AFTER DELETE ON space_calendar_rsvps
BEGIN
    UPDATE space_calendar_events SET sync_seq = NULL WHERE id = OLD.event_id;
END;

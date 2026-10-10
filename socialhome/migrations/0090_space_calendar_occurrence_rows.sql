-- 0090 — Remove the virtual-occurrence rows an older §25.6 sync stored as
-- one-off space calendar events, and keep that shape out for good.
--
-- Until SYNC_SHAPE_VERSION 3 the ``calendar`` sync exporter read the
-- space's events through ``list_events_in_range``, which EXPANDS a
-- recurring event into one record per occurrence, each with the id
-- ``<series id>@<occurrence start iso>`` (``_expand_window``) and that
-- occurrence's start. The receiver stored every such record as a row of its
-- own: a member household showed a weekly series as N separate one-off
-- events (plus the series itself). The exporter now streams stored rows
-- and the receiver skips that id shape, but the rows already stored stay.
--
-- 1. Repair: delete every ``space_calendar_events`` row whose id is
--    ``<prefix>@<its own start_dt>`` — the exact shape ``_expand_window``
--    mints, and the receiver stored ``start_dt`` from the record's
--    ``start`` (the same ISO string as the id's suffix). With it go what
--    hangs off those ids (no FK, so by hand, as the 0085 tombstone trigger
--    does): ``space_calendar_rsvps``, ``space_calendar_rsvp_reminders``,
--    ``pending_federated_rsvps``, and a personal-calendar mirror
--    (``calendar_events.mirrored_from``, its RSVPs by cascade) a member
--    made by RSVPing "going" to one.
-- 2. Guard: ``space_calendar_events_no_occurrence_rows`` — a BEFORE INSERT
--    trigger that IGNOREs (not ABORTs: a batched writer must not fail its
--    other writes) a row of that shape, so an older provider's stream or
--    any future code path cannot store one again. The series row the
--    occurrences came from carries no ``@<start>`` suffix and is untouched;
--    its ``rrule`` (wiped by the old receiver) comes back on the one full
--    stream ``SYNC_SHAPE_VERSION`` 3 forces.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that writes this table's ids.
--       ``grep -rn "space_calendar_events\|save_event\|tombstone_event"
--       socialhome``: all SQL lives in ``SqliteSpaceCalendarRepo``. Ids come
--       from (a) ``CalendarService`` space create — ``_mint_event_id``, an
--       owner-bound hex id, or the moderation release's item id (the same
--       minted id); (b) the live ``SPACE_CALENDAR_EVENT_*`` inbound handler
--       and (c) the §25.6 receiver — a peer's id, owner-bound hex from every
--       v_36+ sender, a legacy uuid4 hex before; (d) ``tombstone_event``
--       stubs (no start: ``start_dt`` is ''). ICS / AI imports
--       (``import_event``) write the PERSONAL ``calendar_events`` table, where
--       an ICS ``UID`` like ``xyz@host`` lives in ``client_event_uuid`` —
--       never here. So no legitimate space event id contains ``@``; the
--       predicate is narrower still (``@`` immediately followed by the row's
--       own start, to the end of the id), so even a hand-crafted legacy id
--       like ``meeting@calendar.example.org`` is untouched. Rows referencing
--       an event id: the three RSVP / reminder / pending tables above, the
--       personal mirror's ``mirrored_from``, and ``space_posts.linked_event_id``
--       (the feed card) — the sync receiver never published
--       ``CalendarEventCreated`` for a streamed event, so no card names a
--       stray.
--   (2) Non-migration alternatives considered and rejected: a receiver-side
--       runtime cleanup (client-side self-healing — the owner wants the
--       invariant in the database), skipping the shape on read (the rows
--       would still be exported, backed up and RSVP'd to), relying on the
--       host to tombstone them (the host never held these ids, so it never
--       streams a tombstone for them).
--   (3) Smallest change: a delete-only repair of rows matching an exact
--       shape plus one BEFORE INSERT trigger. No column, no index, no
--       rewrite of any live row.

DELETE FROM calendar_events
 WHERE mirrored_from IN (
    SELECT id FROM space_calendar_events
     WHERE start_dt <> ''
       AND length(id) > length(start_dt) + 1
       AND substr(id, length(id) - length(start_dt)) = '@' || start_dt
 );

DELETE FROM space_calendar_rsvps
 WHERE event_id IN (
    SELECT id FROM space_calendar_events
     WHERE start_dt <> ''
       AND length(id) > length(start_dt) + 1
       AND substr(id, length(id) - length(start_dt)) = '@' || start_dt
 );

DELETE FROM space_calendar_rsvp_reminders
 WHERE event_id IN (
    SELECT id FROM space_calendar_events
     WHERE start_dt <> ''
       AND length(id) > length(start_dt) + 1
       AND substr(id, length(id) - length(start_dt)) = '@' || start_dt
 );

DELETE FROM pending_federated_rsvps
 WHERE event_id IN (
    SELECT id FROM space_calendar_events
     WHERE start_dt <> ''
       AND length(id) > length(start_dt) + 1
       AND substr(id, length(id) - length(start_dt)) = '@' || start_dt
 );

DELETE FROM space_calendar_events
 WHERE start_dt <> ''
   AND length(id) > length(start_dt) + 1
   AND substr(id, length(id) - length(start_dt)) = '@' || start_dt;

CREATE TRIGGER space_calendar_events_no_occurrence_rows
BEFORE INSERT ON space_calendar_events
WHEN NEW.start_dt <> ''
 AND length(NEW.id) > length(NEW.start_dt) + 1
 AND substr(NEW.id, length(NEW.id) - length(NEW.start_dt)) = '@' || NEW.start_dt
BEGIN
    SELECT RAISE(IGNORE);
END;

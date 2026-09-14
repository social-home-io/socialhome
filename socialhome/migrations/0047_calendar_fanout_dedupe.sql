-- 0047_calendar_fanout_dedupe.sql
--
-- Repair duplicated household calendar fan-out rows, then install a
-- DB-level guard so they cannot come back.
--
-- THE BUG
--
-- A household event shared with N members is stored as one
-- ``calendar_events`` row per member's personal calendar, every row
-- stamped with the same client-minted ``client_event_uuid`` (see
-- 0004_calendar_client_event_uuid.sql) so the agenda can group the
-- fan-out back into one card. The SPA was supposed to PATCH those rows
-- on an edit; instead it re-ran the multi-target POST batch, minting a
-- brand-new row on every calendar each time. A group therefore grew a
-- copy per edit — the production report is 12 rows across 5 calendars
-- for a single event. The rows are byte-for-byte redundant: same
-- calendar, same intent uuid, same authorship.
--
-- WHAT THIS DOES, IN ORDER
--
--   1. Map every losing row to its survivor. Survivor = the OLDEST row
--      in the ``(calendar_id, client_event_uuid)`` group, ordered by
--      ``created_at, id`` (``id`` breaks a same-second tie so the choice
--      is deterministic on every install). Scope is strictly
--      ``client_event_uuid IS NOT NULL AND origin = 'local'
--      AND mirrored_from IS NULL``.
--   2. Re-home dependents BEFORE deleting. The only table referencing
--      ``calendar_events.id`` is ``calendar_event_rsvps``
--      (``event_id ... REFERENCES calendar_events(id) ON DELETE CASCADE``,
--      PK ``(event_id, user_id, occurrence_at)`` — 0001_initial.sql:1521),
--      so deleting a loser first would destroy a member's reply.
--      ``INSERT OR IGNORE`` moves each RSVP onto the survivor;
--      ``ORDER BY r.updated_at DESC`` makes the newest answer the one
--      that lands (two *losers* can each hold a reply for the same
--      ``(user_id, occurrence_at)`` — the group was federated as
--      separate invites — and without the ordering whichever row the
--      join happened to emit first would win, resurrecting a stale
--      answer). IGNORE keeps the survivor's own answer when it already
--      holds one.
--   3. Delete the losing rows' remaining RSVPs EXPLICITLY, then the
--      rows. The explicit delete is not belt-and-braces: 0046 runs
--      ``PRAGMA foreign_keys=OFF`` and its restore is a silent no-op on
--      a connection in implicit-transaction mode, so FK enforcement —
--      and with it ``ON DELETE CASCADE`` — may well be off by the time
--      we run. A migration must never depend on ambient connection
--      state; the DELETE is correct either way and leaves no orphans.
--   4. Drop the temp mapping table.
--   5. Create the partial unique index.
--
-- THE THREE PREDICATES ARE DELIBERATELY IDENTICAL
--
-- ``client_event_uuid IS NOT NULL AND origin = 'local' AND
-- mirrored_from IS NULL`` scopes all three of: this migration's de-dup
-- set, the ``ux_calendar_events_fanout`` index below, and
-- ``SqliteCalendarRepo.find_by_client_event_uuid``
-- (``repositories/calendar_repo.py``). They must stay in lock-step. A
-- row inside the index but outside a reader's filter is a row the code
-- cannot see yet the DB constrains — and a row inside the de-dup scope
-- but outside the readers' is a row this migration would delete
-- without any caller ever having claimed it. ``mirrored_from IS NULL``
-- is what keeps space-calendar mirrors (``SpaceRsvpMirrorBridge``
-- writes ``origin='local'`` + ``mirrored_from=source.id``; a later
-- PATCH can stamp a ``client_event_uuid`` on one) out of all three.
--
-- One reader is deliberately NARROWER, and that is fine.
-- ``SqliteCalendarRepo.list_copies_for_client_event_uuids`` — which
-- answers "which rows are copies of this shared event?" for the SPA's
-- edit dialog — additionally requires ``calendars.calendar_type =
-- 'personal'`` on the JOINed calendar row. A partial index cannot
-- carry a predicate on another table, so the constraint could not
-- mirror it; and a fan-out copy is a household personal-calendar
-- concept by definition, so a row on a non-personal calendar is not a
-- copy and must never be handed to a client as an edit target.
-- Narrower is the safe direction: it can only hide rows from a
-- caller. The unsafe direction — a row inside the index or the de-dup
-- scope that no reader can see — is what the three predicates above
-- exist to prevent, and it stays impossible.
--
-- The de-dup MUST precede the index: a unique index built over
-- still-duplicated rows fails, and the add-on then boot-loops on every
-- affected install.
--
-- Note on failure semantics: ``run_migrations`` wraps each migration in
-- ``with conn:``, but ``executescript`` issues an implicit COMMIT before
-- it runs, so a .sql migration is NOT atomic. That is safe here because
-- every step is idempotent and re-runnable: ``DROP TABLE IF EXISTS``,
-- a mapping rebuilt from scratch each time, a delete of rows that are
-- gone on a retry, and ``CREATE UNIQUE INDEX IF NOT EXISTS``. A failure
-- part-way leaves ``schema_version`` un-bumped, so the next boot simply
-- runs it again and completes.
--
-- AUDIT (CLAUDE.md "Before adding a SQL migration, audit the code path")
--
-- 1. Every writer of ``client_event_uuid`` was read:
--
--    * ``CalendarService.create_event`` and ``CalendarService.update_event``
--      — both funnel the value through ``_clean_client_event_uuid``, and
--      both write ``origin='local'`` rows. These are the rows the SPA bug
--      duplicated, and the only rows this migration touches.
--    * ``socialhome/services/federation_inbound/personal_calendar.py``
--      — stamps the *peer's* uuid onto ``origin='remote_invite'`` mirror
--      rows of an event hosted on another household. Two different
--      organisers can legitimately land the same uuid-bearing invite on
--      one calendar, and the master copy is not ours to collapse, so
--      those rows are excluded from BOTH the de-dup and the index by the
--      ``origin = 'local'`` predicate.
--    * ``SpaceRsvpMirrorBridge`` — writes the personal mirror of a space
--      calendar event with ``origin='local'`` and
--      ``mirrored_from = <source event id>`` and a NULL uuid, but
--      ``CalendarService.update_event`` will happily PATCH a
--      ``client_event_uuid`` onto such a row. That row is a mirror of an
--      event whose master copy lives on the space calendar, so it is not
--      a fan-out copy and must not be collapsed into one; the
--      ``mirrored_from IS NULL`` predicate excludes it from the de-dup,
--      from the index, and from the repo reader alike.
--
--    Other referencing surfaces were checked and are unaffected:
--
--    * ``space_posts.linked_event_id`` cannot reference a de-dup
--      candidate — space posts link *space* calendar events, and a
--      personal mirror of a space event carries a non-NULL
--      ``mirrored_from``, which the ``origin='local'`` fan-out set never
--      includes.
--    * ``space_calendar_rsvp_reminders`` is space-only; a personal event
--      has no reminder rows at all (``CalendarEventRemindersView`` 404s
--      for non-space events).
--
-- 2. A non-migration alternative was considered and rejected.
--    Client-side self-healing (collapse the group on the next edit) only
--    repairs a group somebody happens to open again, leaves every
--    untouched event duplicated forever, and — decisively — installs no
--    durable guard, so any future client regression re-creates the mess.
--    Enforcing uniqueness in the service layer alone has the same hole:
--    the invariant belongs where every writer, including a restored
--    backup or a future code path, meets it.
--
-- 3. This is the smallest change that holds. No column is added, no
--    table is rebuilt, no existing row's values are edited. Only rows
--    that are provably redundant (same calendar, same intent uuid, same
--    local origin) are removed, and their dependants are re-homed onto
--    the survivor first so no user data is lost. The guard is a partial
--    INDEX — additive, reversible by dropping it, and inert for every
--    row outside the fan-out set.
--
-- 4. KNOWN LIMITATION — the repair is LOCAL-ONLY. Each duplicate POST
--    also fired ``PERSONAL_CALENDAR_EVENT_CREATED``, so every attendee
--    household already holds N ``remote_invite`` mirrors of the same
--    event, each keyed to a different organiser event id. Those mirrors
--    are deliberately outside this de-dup and outside the index (they
--    are the peer's rows, not ours — see point 1), so they persist after
--    this migration, and a peer RSVPing on a mirror of a copy we just
--    deleted has its reply dropped on arrival (no local event id to
--    match). Collapsing the remote side needs a federation-level repair
--    event and is out of scope here; tracked with the fan-out fix in
--    ``docs/protocol/`` / issue #327.

-- 1. Losing row -> survivor.
DROP TABLE IF EXISTS temp.calendar_fanout_dedupe;
CREATE TEMP TABLE calendar_fanout_dedupe AS
SELECT
    e.id AS loser_id,
    (
        SELECT s.id
          FROM calendar_events s
         WHERE s.calendar_id = e.calendar_id
           AND s.client_event_uuid = e.client_event_uuid
           AND s.client_event_uuid IS NOT NULL
           AND s.origin = 'local'
           AND s.mirrored_from IS NULL
         ORDER BY s.created_at, s.id
         LIMIT 1
    ) AS survivor_id
  FROM calendar_events e
 WHERE e.client_event_uuid IS NOT NULL
   AND e.origin = 'local'
   AND e.mirrored_from IS NULL;

-- A group of one maps to itself; those rows are not losers.
DELETE FROM calendar_fanout_dedupe WHERE survivor_id = loser_id;

-- 2. Re-home the RSVPs before the losers go. Newest answer first, so the
--    most recent reply is the one INSERT keeps; IGNORE then preserves the
--    survivor's own answer over any loser's for the same
--    (user_id, occurrence_at).
INSERT OR IGNORE INTO calendar_event_rsvps
    (event_id, user_id, occurrence_at, status, updated_at)
SELECT d.survivor_id, r.user_id, r.occurrence_at, r.status, r.updated_at
  FROM calendar_event_rsvps r
  JOIN calendar_fanout_dedupe d ON d.loser_id = r.event_id
 ORDER BY r.updated_at DESC;

-- 3. Drop the losers' remaining RSVPs explicitly (NOT via the FK cascade —
--    enforcement may be off; see the header), then the rows themselves.
DELETE FROM calendar_event_rsvps
 WHERE event_id IN (SELECT loser_id FROM calendar_fanout_dedupe);

DELETE FROM calendar_events
 WHERE id IN (SELECT loser_id FROM calendar_fanout_dedupe);

-- 4.
DROP TABLE temp.calendar_fanout_dedupe;

-- 5. The guard. Predicate identical to the de-dup scope above and to
--    ``find_by_client_event_uuid`` — see "THE THREE PREDICATES" header note.
CREATE UNIQUE INDEX IF NOT EXISTS ux_calendar_events_fanout
  ON calendar_events(calendar_id, client_event_uuid)
  WHERE client_event_uuid IS NOT NULL
    AND origin = 'local'
    AND mirrored_from IS NULL;

-- 0082 — System chats: the household chat (and, later, a chat per space)
-- built on group-DM storage.
--
-- A system chat is a ``group_dm`` conversation the household creates
-- itself, not a person: one for the whole household (all local users,
-- never federated) and — from a later migration on — one per space. It
-- reuses everything a group DM already has (messages, seats with their
-- read watermark / mute / notification level, reactions, edit and
-- delete, mentions) and is kept out of the DM inbox and the DM badge.
--
--   * ``conversations.system_scope`` — ``NULL`` for every person-made DM
--     and group (every existing row), ``'household'`` for the household
--     chat, ``'space'`` for a space's chat. Who may read and write a
--     system chat is decided live by ``services/system_chat_policy.py``,
--     never by its seat rows (those only hold per-user state).
--   * ``conversations.space_id`` — the space a ``'space'`` chat belongs
--     to; the chat goes with the space (``ON DELETE CASCADE``). Set if and
--     only if ``system_scope = 'space'``: enforced where system chats are
--     created (``SqliteConversationRepo.create_system_chat``), not by a
--     table CHECK — adding one to ``conversations`` would mean a rebuild.
--   * ``ux_conv_space_chat`` / ``ux_conv_household_chat`` — at most one
--     chat per space and one household chat, so concurrent first opens
--     race into the same row (``INSERT OR IGNORE`` then read back).
--   * ``preferences.feat_household_chat`` — the household toggle, default
--     ON like every other ``feat_*`` column. Off hides the chat and
--     refuses writes; nothing is deleted.
--
-- CLAUDE.md "audit before a migration":
--
--   (1) Audited every code path that touches this data. ``conversations``
--       is written by ``SqliteConversationRepo.create`` (local DMs and
--       groups), ``apply_group_roster`` (a group authority's roster, local
--       or ``DM_GROUP_ROSTER`` inbound) and the inbound ``DM_MESSAGE``
--       first-message path; it is read by ``list_for_user`` (inbox, DM
--       badge via ``CornerService``, guardian view
--       ``list_conversations_for_minor``, ``on_guardian_block``),
--       ``list_fully_left_conversation_ids`` (the DM GC sweeper), the
--       ``DmScope`` / ``DmGroupService`` inbound gates and the realtime /
--       notification fan-out. ``preferences`` is read and written only
--       through ``SqlitePreferencesRepo`` (column allow-list from
--       ``PREFERENCE_SCOPE``). Every DM reader that must not see a
--       system chat filters ``system_scope IS NULL`` in the same change.
--   (2) Non-migration alternatives considered and rejected:
--       * Widen the ``type`` CHECK with ``'household_chat'`` /
--         ``'space_chat'`` — SQLite can't alter a CHECK in place, so it
--         is a full ``conversations`` rebuild (and of every FK child), and
--         every ``type`` branch in the DM code would need a third arm.
--       * An id-prefix convention (``household-…`` / ``space-chat-…``) —
--         a string predicate in every query, no FK to the space, and no
--         way to make "one chat per space" a constraint.
--       * Separate chat tables — a second copy of messages, seats,
--         reactions, unread, mute and notification levels: building it
--         new instead of reusing the group-DM storage.
--       * A household setting outside ``preferences`` — every other
--         household feature toggle (``feat_timetable`` …) lives there,
--         and the SPA's toggle list reads that one row.
--   (3) Smallest possible change. Additive only: two nullable columns
--       with a NULL default (every existing conversation stays a plain DM
--       or group, untouched), two partial unique indexes that cover only
--       system rows, and one ``NOT NULL DEFAULT 1`` toggle (no backfill).
--       No existing row is changed.
ALTER TABLE conversations ADD COLUMN system_scope TEXT
    CHECK (system_scope IS NULL OR system_scope IN ('household', 'space'));

ALTER TABLE conversations ADD COLUMN space_id TEXT
    REFERENCES spaces(id) ON DELETE CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_conv_space_chat
    ON conversations(space_id) WHERE space_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_conv_household_chat
    ON conversations(system_scope) WHERE system_scope = 'household';

ALTER TABLE preferences
    ADD COLUMN feat_household_chat INTEGER NOT NULL DEFAULT 1
        CHECK (feat_household_chat IN (0, 1));

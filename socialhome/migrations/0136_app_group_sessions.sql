-- 0136 — Group sessions for Social Home Apps (group play PR 2, v_76).
-- Follows 0135.
--
-- An app session used to be strictly two people: one row per (app, person,
-- session) in ``app_pending_sessions`` naming exactly one other side. A
-- *table* seats 2–30 people. Its host household (the *hub*) keeps the
-- roster and passes everything on; every other household with a player
-- keeps a copy of the table as the hub last sent it.
--
--   app_group_sessions  One row per table this household knows — the
--       hub's own (authoritative) or a copy. ``hub_instance`` is the host
--       household (fixed for the table's life), ``host_instance`` /
--       ``host_user`` the host person now (may change hands). The roster
--       is ``roster_json`` (``[{seat, instance, user, name, household,
--       state, joined_at}]``, ≤ 60 entries) numbered by ``roster_rev`` (a
--       copy keeps the newest it got). ``seq`` numbers the table's
--       messages and ``seat_key_json`` holds this household's seat key
--       (KEK-wrapped) — both used from group play part II. ``space_id`` is
--       set for a table opened in a space (v_77); NULL for one opened by
--       picking people. No foreign key to ``installed_apps``: a household
--       keeps a space table's card for an app it hasn't installed.
--   app_group_held      The group mailbox (part II): per (app, person,
--       table, sender seat) the latest message from that seat, ≤ 16 KiB.
--       Goes with the person's ``app_pending_sessions`` row (cascade).
--
-- Each local player still has their ``app_pending_sessions`` row
-- (``invited`` → ``active``, ``from_instance`` = the hub, ``from_user`` =
-- the host person, ``payload_json.group = true``): that keeps Needs you,
-- the invite caps, the 14 / 90-day TTLs, the 50-row bound and the
-- uninstall cascade working with no new code paths.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the data. ``app_pending_sessions``
--       (0025, 0131) and every reader in ``AppFederationService``
--       (``_known_session``, ``_plays_with``, ``_hold``, ``take_held``,
--       ``list_contacts``, ``_invite_usage``, ``sessions_ending`` /
--       ``preview_uninstall``, accept / decline / withdraw), ``app_repo``
--       (``add_pending_session``'s refresh rule, ``hold_message``), the
--       hourly prune (``AppPendingSessionPruneScheduler``) and the
--       uninstall cascade. Nothing there can hold a roster: every rule is
--       written for one other side. Group rows are marked
--       (``payload_json.group``, a Social Home-only mark) and kept out of
--       every 1:1 path. ``backup_service`` / ``data_export_service`` don't
--       export app sessions (live game state, not a person's data).
--   (2) Non-migration alternatives, rejected:
--       * the roster in each player's ``app_pending_sessions.payload_json``
--         — copied per local player, no home for a table a household only
--         relays or only sees, no single ``seq``;
--       * held group messages in ``held_json`` as a dict per sender — a
--         read-modify-write per move on a hot row and unbounded JSON;
--       * notification rows as table storage — a table isn't per person;
--       * asking the hub for the table on every view — a request per
--         viewer instead of an event per change.
--   (3) Smallest change: two new tables, additive, no existing row touched,
--       no backfill. Indexes only for the reads that aren't by key: a
--       person's tables per app (caps), and the hourly expiry / prune.

CREATE TABLE app_group_sessions (
    session_id     TEXT PRIMARY KEY,
    app_id         TEXT NOT NULL,
    host_instance  TEXT NOT NULL,
    host_user      TEXT NOT NULL,
    hub_instance   TEXT NOT NULL,
    space_id       TEXT REFERENCES spaces(id) ON DELETE CASCADE,
    state          TEXT NOT NULL CHECK (state IN ('lobby', 'playing', 'ended')),
    end_reason     TEXT,
    min_players    INTEGER NOT NULL CHECK (min_players >= 2),
    max_players    INTEGER NOT NULL CHECK (max_players BETWEEN min_players AND 30),
    late_join      INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    roster_json    TEXT NOT NULL,
    roster_rev     INTEGER NOT NULL,
    seq            INTEGER NOT NULL DEFAULT 0,
    seat_key_json  TEXT,
    created_at     TEXT NOT NULL,   -- UTC ISO 8601
    started_at     TEXT,
    expires_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX idx_app_group_sessions_host
    ON app_group_sessions(app_id, hub_instance, host_user, created_at);
CREATE INDEX idx_app_group_sessions_state
    ON app_group_sessions(state, updated_at);

CREATE TABLE app_group_held (
    app_id      TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    from_seat   INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    held_json   TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (app_id, user_id, session_id, from_seat),
    FOREIGN KEY (app_id, user_id, session_id)
        REFERENCES app_pending_sessions(app_id, user_id, session_id)
        ON DELETE CASCADE
);

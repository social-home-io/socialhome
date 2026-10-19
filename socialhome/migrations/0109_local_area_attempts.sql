-- 0109 — a local community's daily join budgets survive a restart.
-- Follows 0108 (superseded directory verdict).
--
-- Every join to a local community makes its host decide on the location the
-- join carried; the host budgets how often a person (and a remote household)
-- may ask, as spam control (owner decision 2026-10-19):
--
-- * ``person``    — every check a joining PERSON starts (open join, join
--                   request, follow, invite accept, link redeem —
--                   successful ones included), 10 per community per UTC
--                   day. ``subject_id`` is a SHA-256 of
--                   ``household | user id``, never the raw user id.
-- * ``household`` — the same checks summed per REMOTE household, 20 per
--                   community per UTC day: the backstop against a
--                   household minting fresh user ids.
--
-- One row per (space, kind, subject, UTC day); rows older than yesterday
-- are pruned by the repo on every write. It never holds a location.
--
-- Rewritten in place before release (2026-10-19): the first revision also
-- allowed a ``verdict_home`` kind for a directory verdict that no longer
-- exists. A development database that ran it keeps the wider CHECK, which
-- accepts every row the current code writes.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that runs or counts the check:
--       ``require_local_eligibility`` and its callers —
--       ``SpaceService.request_join`` / ``_subscribe_to_space`` /
--       ``accept_local_invite`` / ``accept_invite_token`` (person-started),
--       ``add_member`` / ``invite_local_user`` (admin-started, not
--       counted), ``SpaceInviteTokenRedeemCoordinator`` (remote redeem),
--       ``PrivateInviteHandler._local_area_admits`` (remote accept),
--       ``SpaceInvitesInbound._on_join_request`` (remote request). All of
--       them kept their counters in memory before, lost on every restart.
--   (2) Alternatives rejected: memory (the owner wants the budgets to
--       survive a reboot); a generic rate-limit table — none exists, every
--       HTTP limiter is in memory; ``instance_config`` key/value rows —
--       no atomic per-subject increment, no day column to prune on;
--       a column on ``space_join_requests`` / ``remote_instances`` — not
--       every counted path files a request, and a local person has no
--       ``remote_instances`` row.
--   (3) Smallest change: one new table, no backfill, no rewrite; the
--       primary key is the only index (the table holds a few rows a day).

CREATE TABLE IF NOT EXISTS local_area_attempts (
    space_id     TEXT    NOT NULL,
    subject_kind TEXT    NOT NULL
        CHECK(subject_kind IN ('person', 'household')),
    subject_id   TEXT    NOT NULL,
    day          TEXT    NOT NULL,  -- UTC calendar day, 'YYYY-MM-DD'
    count        INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
    PRIMARY KEY (space_id, subject_kind, subject_id, day)
);

-- 0109 — a local community's daily budgets survive a restart.
-- Follows 0108 (peer space directory area_edge).
--
-- A seat attempt on a local community is an exact inside / outside answer,
-- so the host budgets how often it looks (owner decision 2026-10-19):
--
-- * ``person``    — every eligibility check a joining PERSON starts (open
--                   join, join request, follow, invite accept, link
--                   redeem — successful joins included), 3 per community
--                   per UTC day. ``subject_id`` is a SHA-256 of
--                   ``household | user id``, never the raw user id.
-- * ``household`` — the same checks summed per REMOTE household, 10 per
--                   community per UTC day: the backstop against a
--                   household minting fresh user ids.
-- * ``verdict_home`` — how many different shared homes of one paired
--                   household the directory verdict followed today (4).
--                   ``space_id`` is ``''``: it spans every community.
--
-- One row per (space, kind, subject, UTC day); rows older than yesterday
-- are pruned by the repo on every write. It never holds a location.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that runs or counts the check:
--       ``require_local_eligibility`` and its callers —
--       ``SpaceService.request_join`` / ``_subscribe_to_space`` /
--       ``accept_local_invite`` / ``accept_invite_token`` (person-started),
--       ``add_member`` / ``invite_local_user`` / ``invite_remote_user`` /
--       ``approve_join_request`` (admin-started, not counted),
--       ``SpaceInviteTokenRedeemCoordinator`` (remote redeem),
--       ``PrivateInviteHandler._local_area_admits`` (remote accept),
--       ``SpaceInvitesInbound._on_join_request`` (remote request) — and
--       ``PeerDirectoryService._verdict_home``. All of them kept their
--       counters in memory (#1054), lost on every restart.
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
        CHECK(subject_kind IN ('person', 'household', 'verdict_home')),
    subject_id   TEXT    NOT NULL,
    day          TEXT    NOT NULL,  -- UTC calendar day, 'YYYY-MM-DD'
    count        INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
    PRIMARY KEY (space_id, subject_kind, subject_id, day)
);

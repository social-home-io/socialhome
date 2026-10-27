-- 0128 — a household remembers when a highlight was taken away from it
-- (v_70, "Change for all of today", #1191 adversarial review). Follows 0127.
--
-- Narrowing today's highlight sends ``HIGHLIGHT_DELETED`` to every household
-- that drops out, and a re-sent ``HIGHLIGHT_CREATED`` (its new slice of the
-- audience) to every household that stays. Envelopes are not delivered in
-- order: the outbox retries by ``next_attempt_at`` while live sends go
-- straight out, so a frame sent before the change can land after the
-- removal and recreate the highlight (the lazy-parent path of
-- ``HIGHLIGHT_FRAME_APPENDED``), and an older re-sent slice can land after a
-- newer one and roll the audience back. Hard-deleting the row forgets the
-- removal ever happened. One row per remote highlight a household has been
-- told about by an author household on v_70+:
--
--   audience_at  the author's ``occurred_at`` of the newest audience applied
--                (a CREATED, or the parent fields of a lazily created one) —
--                an older one keeps the stored audience
--   removed_at   the author's ``occurred_at`` of the newest
--                ``HIGHLIGHT_DELETED`` applied — a CREATED / FRAME_APPENDED
--                at or before it is refused, a newer one (the author widened
--                again) recreates the highlight
--   expires_at   the highlight's own retention cutoff: the row goes with the
--                retention prune, as the highlight would have
--
-- Both stamps are the author household's clock, compared only with each
-- other, never with ours.
--
-- CLAUDE.md "audit before a migration":
--   (1) Audited every path that touches the data. ``highlights`` writers:
--       ``find_or_create_today`` / ``set_audience`` (author side, local
--       rows), ``save_highlight`` (the inbound CREATED and lazy-parent
--       APPENDED upsert — the only receiver writer), ``delete_highlight`` /
--       ``delete_by_author`` (inbound DELETED, USER_REMOVED), the retention
--       prunes, ``mark_published`` / ``mark_unpublished`` (author side).
--       Readers: the inbox, detail, author list, publication and signalling
--       reads, the retention prunes. Nothing in the row says when its
--       audience was set by the author (``created_at`` is our arrival time,
--       used for ordering and the composer's "chose at"), and a deleted
--       highlight has no row at all.
--   (2) Non-migration alternatives, rejected: keeping the ``highlights`` row
--       as a tombstone (the moments precedent, 0057) puts a frame-less row
--       in front of every reader listed above — inbox, profile list,
--       ``prune_over_max`` counts, ``list_authors_with_highlights``,
--       published lists — each needing a new filter, where a missed one
--       shows an empty highlight or miscounts; an in-memory record is lost
--       on restart while the sender's outbox keeps retrying for up to 7 days;
--       reusing ``created_at`` as the audience stamp breaks the inbox order.
--       The sender also drops the household's queued envelopes for the
--       highlight before a removal (outbox ``supersede_key`` prefix), which
--       protects receivers older than v_70 — but not against a frame already
--       in flight, or an older re-sent slice delivered live.
--   (3) Smallest change: one new additive table, no change to any existing
--       row or column; no FK to ``highlights`` (it must outlive the row it
--       stamps); one index for the retention prune. Local-only, never
--       federated, not in the backup's or export's highlight data.
CREATE TABLE IF NOT EXISTS highlight_stamps (
    highlight_id    TEXT PRIMARY KEY,
    author_user_id  TEXT NOT NULL,
    audience_at     TEXT,
    removed_at      TEXT,
    expires_at      TEXT NOT NULL,
    CHECK (audience_at IS NOT NULL OR removed_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_highlight_stamps_expires
    ON highlight_stamps(expires_at);
